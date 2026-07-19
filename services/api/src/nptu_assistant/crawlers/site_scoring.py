from __future__ import annotations

from collections.abc import Sequence
import math
import re
from typing import Protocol
import unicodedata
from urllib.parse import unquote, urlsplit

from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage
from nptu_assistant.crawlers.config import SiteSearchScoringWeights
from nptu_assistant.crawlers.site_models import (
    CandidatePage,
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
)
from nptu_assistant.providers.protocols import EmbeddingProvider
from nptu_assistant.rag.embedding_cache import RetrievalExecutionContext


_SEARCH_SEPARATOR = re.compile(r"[^0-9a-z\u3400-\u9fff]+", re.IGNORECASE)


def _normalize(value: str) -> str:
    return _SEARCH_SEPARATOR.sub("", unicodedata.normalize("NFKC", value).casefold())


def _ngrams(value: str) -> set[str]:
    normalized = _normalize(value)
    if not normalized:
        return set()
    if len(normalized) < 2:
        return {normalized}
    grams: set[str] = set()
    for size in range(2, min(4, len(normalized)) + 1):
        grams.update(
            normalized[index : index + size]
            for index in range(len(normalized) - size + 1)
        )
    return grams


def _similarity(left: str, right: str) -> float:
    left_grams = _ngrams(left)
    right_grams = _ngrams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _max_similarity(queries: Sequence[str], value: str) -> float:
    return max((_similarity(query, value) for query in queries), default=0.0)


def _phrase_score(queries: Sequence[str], value: str) -> float:
    normalized_value = _normalize(value)
    if not normalized_value:
        return 0.0
    scores: list[float] = []
    for query in queries:
        normalized_query = _normalize(query)
        if normalized_query and normalized_query in normalized_value:
            scores.append(1.0)
    return max(scores, default=0.0)


def _concept_coverage(concepts: Sequence[str], value: str) -> float:
    normalized_value = _normalize(value)
    if not normalized_value or not concepts:
        return 0.0
    matches = sum(
        1
        for concept in concepts
        if _normalize(concept) and _normalize(concept) in normalized_value
    )
    return matches / len(concepts)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(0.0, min(1.0, numerator / (left_norm * right_norm)))


class CandidateScorer(Protocol):
    def score_candidate(self, plan: SearchPlan, candidate: CandidatePage) -> float: ...

    def score_pages(
        self,
        plan: SearchPlan,
        candidates: Sequence[CandidatePage],
        pages: Sequence[NptuSitePage],
        *,
        deadline: SearchDeadline | None = None,
        execution_context: RetrievalExecutionContext | None = None,
    ) -> list[float]: ...


class HybridCandidateScorer:
    def __init__(
        self,
        weights: SiteSearchScoringWeights,
        embedding_provider: EmbeddingProvider,
        *,
        batch_size: int = 32,
    ) -> None:
        if batch_size < 1:
            raise ValueError("embedding batch size 必須大於零")
        self._weights = weights
        self._embedding_provider = embedding_provider
        self._batch_size = batch_size

    def score_candidate(self, plan: SearchPlan, candidate: CandidatePage) -> float:
        queries = plan.retrieval_queries
        url_text = unquote(urlsplit(candidate.url).path)
        values = {
            "phrase": _phrase_score(queries, f"{candidate.anchor_text} {url_text}"),
            "anchor": max(
                _max_similarity(queries, candidate.anchor_text),
                _concept_coverage(plan.concepts, candidate.anchor_text),
            ),
            "url": max(
                _max_similarity(queries, url_text),
                _concept_coverage(plan.concepts, url_text),
            ),
            "parent": candidate.parent_relevance,
            "discovery": candidate.discovery_relevance,
        }
        weights = {
            "phrase": self._weights.phrase,
            "anchor": self._weights.anchor,
            "url": self._weights.url,
            "parent": self._weights.parent,
            "discovery": self._weights.discovery,
        }
        available = sum(weights.values()) or 1.0
        score = (
            sum(values[name] * weight for name, weight in weights.items()) / available
        )
        return max(0.0, min(1.0, score - candidate.depth * self._weights.depth_penalty))

    def score_pages(
        self,
        plan: SearchPlan,
        candidates: Sequence[CandidatePage],
        pages: Sequence[NptuSitePage],
        *,
        deadline: SearchDeadline | None = None,
        execution_context: RetrievalExecutionContext | None = None,
    ) -> list[float]:
        if len(candidates) != len(pages):
            raise ValueError("候選頁面與已擷取頁面數量不一致")
        semantic_scores = self._semantic_scores(
            plan,
            pages,
            deadline=deadline,
            execution_context=execution_context,
        )
        queries = plan.retrieval_queries
        positive_weight = (
            self._weights.phrase
            + self._weights.title
            + self._weights.heading
            + self._weights.anchor
            + self._weights.url
            + self._weights.body
            + self._weights.lexical
            + self._weights.semantic
            + self._weights.parent
            + self._weights.discovery
        ) or 1.0
        scores: list[float] = []
        for candidate, page, semantic in zip(
            candidates,
            pages,
            semantic_scores,
            strict=True,
        ):
            headings = " ".join(page.headings)
            url_text = unquote(urlsplit(page.canonical_url).path)
            body = page.body[:8_000]
            phrase = max(
                _phrase_score(queries, page.title),
                _phrase_score(queries, headings),
                _phrase_score(queries, body),
            )
            title = max(
                _max_similarity(queries, page.title),
                _concept_coverage(plan.concepts, page.title),
            )
            heading = max(
                _max_similarity(queries, headings),
                _concept_coverage(plan.concepts, headings),
            )
            anchor = max(
                _max_similarity(queries, candidate.anchor_text),
                _concept_coverage(plan.concepts, candidate.anchor_text),
            )
            url = max(
                _max_similarity(queries, url_text),
                _concept_coverage(plan.concepts, url_text),
            )
            body_score = _concept_coverage(plan.concepts, body)
            lexical = max(_max_similarity(queries, body), title, heading, anchor, url)
            weighted = (
                phrase * self._weights.phrase
                + title * self._weights.title
                + heading * self._weights.heading
                + anchor * self._weights.anchor
                + url * self._weights.url
                + body_score * self._weights.body
                + lexical * self._weights.lexical
                + semantic * self._weights.semantic
                + candidate.parent_relevance * self._weights.parent
                + candidate.discovery_relevance * self._weights.discovery
            ) / positive_weight
            weighted -= candidate.depth * self._weights.depth_penalty
            scores.append(max(0.0, min(1.0, weighted)))
        return scores

    def _semantic_scores(
        self,
        plan: SearchPlan,
        pages: Sequence[NptuSitePage],
        *,
        deadline: SearchDeadline | None = None,
        execution_context: RetrievalExecutionContext | None = None,
    ) -> list[float]:
        if not pages:
            return []
        page_texts = [
            "\n".join((page.title, " ".join(page.headings), page.body[:4_000]))
            for page in pages
        ]
        try:
            if deadline is not None:
                deadline.raise_if_expired()
            first_page_count = max(0, self._batch_size - 1)
            texts = [plan.semantic_text, *page_texts[:first_page_count]]
            first_vectors = (
                execution_context.embed(
                    self._embedding_provider,
                    texts,
                    deadline=deadline,
                )
                if execution_context is not None
                else self._embedding_provider.embed(
                    texts,
                    timeout_seconds=(
                        deadline.remaining_seconds() if deadline is not None else None
                    ),
                )
            )
            if deadline is not None:
                deadline.raise_if_expired()
        except SearchDeadlineExceeded:
            raise
        except Exception:
            return [0.0 for _page in pages]
        if not first_vectors:
            return [0.0 for _page in pages]
        query_vector = first_vectors[0]
        page_vectors = list(first_vectors[1:])
        try:
            for start in range(first_page_count, len(page_texts), self._batch_size):
                if deadline is not None:
                    deadline.raise_if_expired()
                page_vectors.extend(
                    self._embedding_provider.embed(
                        page_texts[start : start + self._batch_size],
                        timeout_seconds=(
                            deadline.remaining_seconds()
                            if deadline is not None
                            else None
                        ),
                    )
                )
                if deadline is not None:
                    deadline.raise_if_expired()
        except SearchDeadlineExceeded:
            raise
        except Exception:
            return [0.0 for _page in pages]
        if len(page_vectors) != len(page_texts):
            return [0.0 for _page in pages]
        return [_cosine(query_vector, vector) for vector in page_vectors]
