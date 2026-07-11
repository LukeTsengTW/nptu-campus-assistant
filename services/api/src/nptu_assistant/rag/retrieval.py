from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.db.models import Announcement, Document, DocumentChunk, Source
from nptu_assistant.providers.protocols import EmbeddingProvider
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.routing import QuestionRoute


class SqlRetriever:
    def __init__(
        self,
        factory: sessionmaker[Session],
        embedding_provider: EmbeddingProvider,
    ) -> None:
        self._factory = factory
        self._embedding_provider = embedding_provider

    def search(self, question: str, route: QuestionRoute) -> list[Evidence]:
        results: list[Evidence] = []
        if route in {QuestionRoute.DOCUMENT, QuestionRoute.MIXED}:
            results.extend(self._search_documents(question))
        if route in {QuestionRoute.ANNOUNCEMENT, QuestionRoute.MIXED}:
            results.extend(self._search_announcements(question))
        return sorted(results, key=lambda item: item.score, reverse=True)[:20]

    def _search_documents(self, question: str) -> list[Evidence]:
        vector = self._embedding_provider.embed([question])[0]
        vector_score = (1 - DocumentChunk.embedding.cosine_distance(vector)).label("score")
        keyword_score = func.greatest(
            func.similarity(DocumentChunk.content, question),
            func.similarity(Document.title, question),
        ).label("score")
        base_columns = (DocumentChunk, Document, Source)
        with self._factory() as session:
            vector_rows = session.execute(
                select(*base_columns, vector_score)
                .join(Document, Document.id == DocumentChunk.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(Document.is_current.is_(True))
                .order_by(desc("score"))
                .limit(20)
            ).all()
            keyword_rows = session.execute(
                select(*base_columns, keyword_score)
                .join(Document, Document.id == DocumentChunk.document_id)
                .join(Source, Source.id == Document.source_id)
                .where(Document.is_current.is_(True))
                .order_by(desc("score"))
                .limit(20)
            ).all()
        return self._rrf_merge(vector_rows, keyword_rows)

    @staticmethod
    def _rrf_merge(vector_rows: list[object], keyword_rows: list[object]) -> list[Evidence]:
        ranks: dict[str, float] = defaultdict(float)
        raw_scores: dict[str, float] = defaultdict(float)
        records: dict[str, tuple[DocumentChunk, Document, Source]] = {}
        for rows in (vector_rows, keyword_rows):
            for rank, row in enumerate(rows, start=1):
                chunk, document, source, raw_score = row
                key = str(chunk.id)
                records[key] = (chunk, document, source)
                ranks[key] += 1.0 / (60 + rank)
                raw_scores[key] = max(raw_scores[key], max(0.0, min(1.0, float(raw_score or 0.0))))
        evidence = []
        for key, (chunk, document, source) in records.items():
            rrf_component = min(1.0, ranks[key] * 30.0)
            score = (raw_scores[key] * 0.8) + (rrf_component * 0.2)
            evidence.append(
                Evidence(
                    id=key,
                    kind=AnswerType.OFFICIAL_DOCUMENT,
                    title=document.title,
                    url=document.canonical_url,
                    unit=source.unit,
                    published_at=document.published_at,
                    content=chunk.content,
                    score=score,
                )
            )
        return sorted(evidence, key=lambda item: item.score, reverse=True)[:6]

    def _search_announcements(self, question: str) -> list[Evidence]:
        keyword = re.sub(r"(最新|最近|近期|公告|有哪些|請問|？|\?|，|。)", "", question).strip()
        score_expression = func.greatest(
            func.similarity(Announcement.title, keyword),
            func.similarity(Announcement.body, keyword),
        ).label("score")
        with self._factory() as session:
            if keyword:
                rows = session.execute(
                    select(Announcement, score_expression)
                    .order_by(desc("score"), Announcement.published_at.desc())
                    .limit(20)
                ).all()
            else:
                rows = [
                    (item, 0.65)
                    for item in session.scalars(
                        select(Announcement).order_by(Announcement.published_at.desc()).limit(20)
                    ).all()
                ]
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
        ]
