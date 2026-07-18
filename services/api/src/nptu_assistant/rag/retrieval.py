from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import date
from typing import cast

from sqlalchemy import case, desc, func, literal, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
    search_query_key,
)
from nptu_assistant.db.models import Announcement, Document, DocumentChunk, Source
from nptu_assistant.providers.protocols import EmbeddingProvider
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.tools import AnnouncementSort


FAILED_SEARCH_MIN_SIMILARITY = 0.1
DOCUMENT_QUERY_CANDIDATE_LIMIT = 20
DOCUMENT_RRF_K = 60
DOCUMENT_RAW_SCORE_WEIGHT = 0.72
DOCUMENT_RRF_SCORE_WEIGHT = 0.20
DOCUMENT_CONCEPT_COVERAGE_WEIGHT = 0.06
DOCUMENT_EXACT_TITLE_WEIGHT = 0.02


def _public_announcement_source_filter() -> object:
    return Source.name.not_ilike("%fixture%")


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 20:
        raise ValueError("limit 必須介於 1 到 20")


class SqlRetriever:
    def __init__(
        self,
        factory: sessionmaker[Session],
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._factory = factory
        self._embedding_provider = embedding_provider

    def search_documents(self, *, query: str, limit: int = 6) -> list[Evidence]:
        query = query.strip()
        if not query:
            raise ValueError("文件 query 不得為空")
        return self.search_documents_with_plan(
            plan=SearchPlan.from_query(query, limit=limit),
            limit=limit,
        )

    def search_documents_with_plan(
        self,
        *,
        plan: SearchPlan,
        limit: int = 6,
        deadline: SearchDeadline | None = None,
    ) -> list[Evidence]:
        _validate_limit(limit)
        if deadline is not None:
            deadline.raise_if_expired()
        queries = plan.retrieval_queries
        try:
            vectors = self._embedding_provider.embed(
                list(queries),
                timeout_seconds=(deadline.remaining_seconds() if deadline else None),
            )
        except Exception as exc:
            if deadline is not None and deadline.expired():
                raise SearchDeadlineExceeded(
                    "文件檢索 embedding 已耗盡網站搜尋時間額度"
                ) from exc
            raise
        if deadline is not None:
            deadline.raise_if_expired()
        if len(vectors) != len(queries):
            raise ValueError("文件查詢與 embedding 數量不一致")
        base_columns = (DocumentChunk, Document, Source)
        ranked_rows: list[list[object]] = []
        with self._factory() as session:
            for query, vector in zip(queries, vectors, strict=True):
                if deadline is not None:
                    deadline.raise_if_expired()
                vector_score = (
                    1 - DocumentChunk.embedding.cosine_distance(vector)
                ).label("score")
                keyword_score = func.greatest(
                    func.similarity(DocumentChunk.content, query),
                    func.similarity(Document.title, query),
                ).label("score")
                ranked_rows.append(
                    list(
                        session.execute(
                            select(*base_columns, vector_score)
                            .join(Document, Document.id == DocumentChunk.document_id)
                            .join(Source, Source.id == Document.source_id)
                            .where(Document.is_current.is_(True))
                            .order_by(desc("score"))
                            .limit(DOCUMENT_QUERY_CANDIDATE_LIMIT)
                        ).all()
                    )
                )
                if deadline is not None:
                    deadline.raise_if_expired()
                ranked_rows.append(
                    list(
                        session.execute(
                            select(*base_columns, keyword_score)
                            .join(Document, Document.id == DocumentChunk.document_id)
                            .join(Source, Source.id == Document.source_id)
                            .where(Document.is_current.is_(True))
                            .order_by(desc("score"))
                            .limit(DOCUMENT_QUERY_CANDIDATE_LIMIT)
                        ).all()
                    )
                )
                if deadline is not None:
                    deadline.raise_if_expired()
        return self._rrf_merge(
            ranked_rows,
            queries=queries,
            concepts=tuple(plan.concepts),
            limit=limit,
        )

    @staticmethod
    def _rrf_merge(
        ranked_rows: list[list[object]],
        *,
        queries: tuple[str, ...] = (),
        concepts: tuple[str, ...] = (),
        limit: int = 6,
    ) -> list[Evidence]:
        ranks: dict[str, float] = defaultdict(float)
        raw_scores: dict[str, float] = defaultdict(float)
        records: dict[str, tuple[DocumentChunk, Document, Source]] = {}
        for rows in ranked_rows:
            for rank, row in enumerate(rows, start=1):
                chunk, document, source, raw_score = cast(
                    tuple[DocumentChunk, Document, Source, float],
                    row,
                )
                key = str(chunk.id)
                records[key] = (chunk, document, source)
                ranks[key] += 1.0 / (DOCUMENT_RRF_K + rank)
                raw_scores[key] = max(
                    raw_scores[key],
                    max(0.0, min(1.0, float(raw_score or 0.0))),
                )
        scored_records: list[tuple[float, str, Evidence]] = []
        list_count = max(1, len(ranked_rows))
        for key, (chunk, document, source) in records.items():
            rrf_component = min(
                1.0,
                ranks[key] * (DOCUMENT_RRF_K + 1) / list_count,
            )
            searchable = " ".join((document.title, chunk.content, source.unit or ""))
            normalized_searchable = search_query_key(searchable)
            concept_keys = [
                value for concept in concepts if (value := search_query_key(concept))
            ]
            concept_coverage = (
                sum(value in normalized_searchable for value in concept_keys)
                / len(concept_keys)
                if concept_keys
                else 0.0
            )
            normalized_title = search_query_key(document.title)
            exact_title = float(
                any(
                    (query_key := search_query_key(query))
                    and (query_key in normalized_title or normalized_title in query_key)
                    for query in queries
                )
            )
            score = max(
                0.0,
                min(
                    1.0,
                    raw_scores[key] * DOCUMENT_RAW_SCORE_WEIGHT
                    + rrf_component * DOCUMENT_RRF_SCORE_WEIGHT
                    + concept_coverage * DOCUMENT_CONCEPT_COVERAGE_WEIGHT
                    + exact_title * DOCUMENT_EXACT_TITLE_WEIGHT,
                ),
            )
            scored_records.append(
                (
                    score,
                    str(document.id),
                    Evidence(
                        id=key,
                        kind=AnswerType.OFFICIAL_DOCUMENT,
                        title=document.title,
                        url=document.canonical_url,
                        unit=source.unit,
                        published_at=document.published_at,
                        content=chunk.content,
                        score=score,
                    ),
                )
            )
        evidence: list[Evidence] = []
        used_documents: set[str] = set()
        for _score, document_id, item in sorted(
            scored_records,
            key=lambda value: value[0],
            reverse=True,
        ):
            if document_id in used_documents:
                continue
            evidence.append(item)
            used_documents.add(document_id)
            if len(evidence) >= limit:
                break
        return evidence

    def search_announcements(
        self,
        *,
        query: str | None,
        limit: int,
        sort: AnnouncementSort,
        unit: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        canonical_urls: tuple[str, ...] | None = None,
    ) -> list[Evidence]:
        _validate_limit(limit)
        if date_from and date_to and date_from > date_to:
            raise ValueError("起始日期不得晚於結束日期")
        if canonical_urls is not None:
            canonical_urls = tuple(dict.fromkeys(canonical_urls))
        if canonical_urls == ():
            return []

        query = query.strip() if query else ""
        unit = unit.strip() if unit else None
        fallback_relevance_filter: object | None = None
        if query:
            raw_score_expression = func.greatest(
                func.similarity(Announcement.title, query),
                func.similarity(Announcement.body, query),
                func.similarity(func.coalesce(Announcement.unit, ""), query),
            )
            score_expression = raw_score_expression.label("score")
            fallback_relevance_filter = (
                raw_score_expression >= FAILED_SEARCH_MIN_SIMILARITY
            )
        else:
            score_expression = literal(0.65).label("score")
        filters = (
            [] if canonical_urls is not None else [_public_announcement_source_filter()]
        )
        if canonical_urls is not None:
            filters.append(Announcement.canonical_url.in_(canonical_urls))
        elif fallback_relevance_filter is not None:
            filters.append(fallback_relevance_filter)
        if unit and canonical_urls is None:
            filters.append(Announcement.unit.ilike(f"%{unit}%"))
        if date_from:
            filters.append(Announcement.published_at >= date_from)
        if date_to:
            filters.append(Announcement.published_at <= date_to)

        statement = (
            select(Announcement, score_expression)
            .join(Source, Source.id == Announcement.source_id)
            .where(*filters)
        )
        canonical_order = (
            case(
                {url: index for index, url in enumerate(canonical_urls)},
                value=Announcement.canonical_url,
                else_=len(canonical_urls),
            )
            if canonical_urls is not None
            else None
        )
        effective_sort = (
            sort
            if query or sort is not AnnouncementSort.RELEVANCE
            else AnnouncementSort.NEWEST
        )
        if effective_sort is AnnouncementSort.RELEVANCE:
            order = [desc("score"), Announcement.published_at.desc()]
        elif effective_sort is AnnouncementSort.OLDEST:
            order = [Announcement.published_at.asc()]
        else:
            order = [Announcement.published_at.desc()]
            if canonical_urls is None:
                order.append(Announcement.last_crawled_at.desc())
        if canonical_order is not None:
            order.append(canonical_order)
        statement = statement.order_by(*order)

        with self._factory() as session:
            rows = session.execute(
                statement if canonical_urls is not None else statement.limit(limit)
            ).all()
        return [
            Evidence(
                id=str(item.id),
                kind=AnswerType.ANNOUNCEMENT,
                title=item.title,
                url=item.canonical_url,
                unit=item.unit,
                published_at=item.published_at,
                content=item.body,
                score=max(0.0, min(1.0, float(score or 0.0))),
            )
            for item, score in rows
        ][:limit]

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        try:
            parsed_id = uuid.UUID(announcement_id)
        except (ValueError, AttributeError):
            return None
        with self._factory() as session:
            item = session.scalar(
                select(Announcement)
                .join(Source, Source.id == Announcement.source_id)
                .where(
                    Announcement.id == parsed_id,
                    _public_announcement_source_filter(),
                )
            )
        if item is None:
            return None
        return Evidence(
            id=str(item.id),
            kind=AnswerType.ANNOUNCEMENT,
            title=item.title,
            url=item.canonical_url,
            unit=item.unit,
            published_at=item.published_at,
            content=item.body,
            score=1.0,
        )
