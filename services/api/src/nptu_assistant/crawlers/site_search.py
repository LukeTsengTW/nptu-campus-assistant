from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import heapq
import json
import logging
import re
import time
from typing import Protocol
from urllib.parse import urlsplit

from pydantic import HttpUrl

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    NptuSitePageAdapter,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.adapters.nptu_search import AnnouncementSearchResult
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.official_units import DocumentSearchScope
from nptu_assistant.crawlers.site_discovery import SiteDiscovery
from nptu_assistant.crawlers.site_map import SiteMapService
from nptu_assistant.crawlers.site_models import (
    CandidatePage,
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchDiagnostics,
    SearchPlan,
)
from nptu_assistant.crawlers.site_scoring import CandidateScorer, HybridCandidateScorer
from nptu_assistant.crawlers.site_search_cache import (
    InMemorySiteSearchCache,
    SiteSearchCache,
    SingleFlightLease,
    SingleFlightRunner,
)
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.ingestion.chunking import TextChunk, chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.providers.protocols import EmbeddingProvider
from nptu_assistant.rag.embedding_cache import RetrievalExecutionContext


SITE_SEARCH_PARTIAL_WARNING = "NPTU 網域搜尋有部分頁面無法取得，結果可能不完整。"
SITE_SEARCH_FAILURE_WARNING = (
    "NPTU 網域搜尋目前無法取得頁面，以下內容來自資料庫既有資料。"
)
SITE_SEARCH_SCORING_VERSION = "p2-v1"
SITE_SEARCH_WAITER_BACKOFF_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.5)

logger = logging.getLogger(__name__)


def site_search_cache_key(payload: Mapping[str, object]) -> str:
    canonical_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class SiteSearchHttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class SiteSearchResult:
    pages: tuple[NptuSitePage, ...]
    diagnostics: SearchDiagnostics

    @property
    def visited_count(self) -> int:
        return self.diagnostics.visited_count

    @property
    def failed_count(self) -> int:
        return self.diagnostics.failed_count

    @property
    def query_relevant_failed_count(self) -> int:
        return self.diagnostics.query_relevant_failed_count


class _ZeroEmbeddingProvider:
    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        return [[0.0] for _text in texts]


class NptuSiteSearchService:
    def __init__(
        self,
        config: SiteSearchConfig,
        http_client: SiteSearchHttpClient,
        adapter: NptuSitePageAdapter | None = None,
        scorer: CandidateScorer | None = None,
        discovery: SiteDiscovery | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        cache: SiteSearchCache | None = None,
        single_flight: SingleFlightRunner | None = None,
        site_map: SiteMapService | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("NPTU 網域搜尋設定尚未啟用")
        self._config = config
        self._http = http_client
        self._adapter = adapter or NptuSitePageAdapter()
        self._scorer = scorer or HybridCandidateScorer(
            config.weights,
            _ZeroEmbeddingProvider(),
        )
        self._discovery = discovery
        self._clock = clock
        self._sleep = sleep
        self._result_cache = cache or InMemorySiteSearchCache(clock=clock)
        self._single_flight = single_flight
        self._site_map = site_map
        self._page_cache: dict[str, tuple[float, NptuSitePage]] = {}

    @property
    def config(self) -> SiteSearchConfig:
        return self._config

    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(
            self._config.query_timeout_seconds,
            clock=self._clock,
        )

    def fetch_page(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str],
        deadline: SearchDeadline,
    ) -> NptuSitePage:
        canonical_url = canonicalize_nptu_url(url)
        if not is_allowed_source_url(canonical_url, allowed_hosts):
            raise ValueError("網站頁面不在來源 host allowlist")
        deadline.raise_if_expired()
        cached_page = self._page_cache.get(canonical_url)
        if (
            cached_page is not None
            and self._config.cache_ttl_seconds
            and self._clock() - cached_page[0] <= self._config.cache_ttl_seconds
        ):
            return cached_page[1]
        get_html = getattr(self._http, "get_html", self._http.get)
        content = get_html(
            canonical_url,
            allowed_hosts=allowed_hosts,
            timeout_seconds=deadline.remaining_seconds(),
            deadline=deadline,
        )
        deadline.raise_if_expired()
        page = self._adapter.parse_page(
            content,
            canonical_url,
            allowed_hosts=list(allowed_hosts),
        )
        deadline.raise_if_expired()
        if self._config.cache_ttl_seconds:
            self._page_cache[canonical_url] = (self._clock(), page)
        return page

    def _cache_payload(
        self,
        search_plan: SearchPlan,
        *,
        limit: int,
        use_discovery: bool,
        scope: DocumentSearchScope | None,
    ) -> dict[str, object]:
        allowed_hosts = (
            scope.allowed_hosts if scope is not None else self._config.allowed_hosts
        )
        seed_urls = scope.seed_urls if scope is not None else self._config.seed_urls
        effective_discovery = use_discovery
        return {
            "schema": SITE_SEARCH_SCORING_VERSION,
            "query": search_plan.query,
            "variants": list(search_plan.retrieval_queries[1:]),
            "concepts": list(search_plan.concepts),
            "limit": limit,
            "canonical_unit": scope.canonical_unit if scope is not None else None,
            "homepage_url": scope.homepage_url if scope is not None else None,
            "allowed_hosts": list(allowed_hosts),
            "preferred_hosts": list(scope.preferred_hosts if scope is not None else ()),
            "seed_urls": list(seed_urls),
            "discovery_enabled": effective_discovery,
            "scoring_version": SITE_SEARCH_SCORING_VERSION,
        }

    def _cache_key(
        self,
        search_plan: SearchPlan,
        *,
        limit: int,
        use_discovery: bool,
        scope: DocumentSearchScope | None,
    ) -> str:
        return site_search_cache_key(
            self._cache_payload(
                search_plan,
                limit=limit,
                use_discovery=use_discovery,
                scope=scope,
            )
        )

    def search(
        self,
        plan: SearchPlan | str,
        *,
        max_items: int | None = None,
        use_discovery: bool = True,
        deadline: SearchDeadline | None = None,
        scope: DocumentSearchScope | None = None,
        execution_context: RetrievalExecutionContext | None = None,
    ) -> SiteSearchResult:
        search_deadline = deadline or self.new_deadline()
        requested_limit = self._config.max_items if max_items is None else max_items
        limit = min(requested_limit, self._config.max_items)
        search_plan = (
            SearchPlan.from_query(plan, limit=limit) if isinstance(plan, str) else plan
        )
        if search_plan.limit != limit:
            search_plan = search_plan.model_copy(update={"limit": limit})
        effective_discovery = use_discovery
        cache_key = self._cache_key(
            search_plan,
            limit=limit,
            use_discovery=use_discovery,
            scope=scope,
        )
        cached_entry = self._result_cache.get(cache_key)
        if cached_entry is not None:
            return cached_entry.result

        lease: SingleFlightLease | None = None
        if self._single_flight is not None:
            lease = self._single_flight.acquire(cache_key)
            if lease is None:
                backoff_index = 0
                while not search_deadline.expired():
                    cached_entry = self._result_cache.get(cache_key)
                    if cached_entry is not None:
                        return cached_entry.result
                    lease = self._single_flight.acquire(cache_key)
                    if lease is not None:
                        break
                    delay = SITE_SEARCH_WAITER_BACKOFF_SECONDS[
                        min(
                            backoff_index,
                            len(SITE_SEARCH_WAITER_BACKOFF_SECONDS) - 1,
                        )
                    ]
                    self._sleep(min(delay, search_deadline.remaining_seconds()))
                    backoff_index += 1
                if lease is None:
                    return SiteSearchResult(
                        (),
                        SearchDiagnostics(query_timed_out=True),
                    )
            try:
                cached_entry = self._result_cache.get(cache_key)
                if cached_entry is not None:
                    return cached_entry.result
                result = self._search_uncached(
                    search_plan,
                    limit=limit,
                    use_discovery=effective_discovery,
                    deadline=search_deadline,
                    scope=scope,
                    execution_context=execution_context,
                )
                if (
                    self._config.cache_ttl_seconds
                    and not result.diagnostics.query_timed_out
                    and result.diagnostics.failed_count == 0
                ):
                    self._result_cache.set(
                        cache_key,
                        result,
                        self._config.cache_ttl_seconds,
                    )
                return result
            finally:
                lease.release()

        result = self._search_uncached(
            search_plan,
            limit=limit,
            use_discovery=effective_discovery,
            deadline=search_deadline,
            scope=scope,
            execution_context=execution_context,
        )
        if (
            self._config.cache_ttl_seconds
            and not result.diagnostics.query_timed_out
            and result.diagnostics.failed_count == 0
        ):
            self._result_cache.set(cache_key, result, self._config.cache_ttl_seconds)
        return result

    def _search_uncached(
        self,
        search_plan: SearchPlan,
        *,
        limit: int,
        use_discovery: bool,
        deadline: SearchDeadline,
        scope: DocumentSearchScope | None,
        execution_context: RetrievalExecutionContext | None,
    ) -> SiteSearchResult:
        search_deadline = deadline
        allowed_hosts = (
            scope.allowed_hosts if scope is not None else self._config.allowed_hosts
        )
        seed_urls = scope.seed_urls if scope is not None else self._config.seed_urls
        effective_discovery = use_discovery

        queue: list[tuple[float, int, CandidatePage]] = []
        queued_scores: dict[str, float] = {}
        discovered_urls: set[str] = set()
        visited: set[str] = set()
        pages: list[NptuSitePage] = []
        candidates: list[CandidatePage] = []
        preliminary_scores: list[float] = []
        fetch_failure_scores: list[float] = []
        timed_out_scores: list[float] = []
        skipped_candidate_count = 0
        query_timed_out = False
        pages_per_host: dict[str, int] = {}
        sequence = 0

        def enqueue(candidate: CandidatePage) -> None:
            nonlocal sequence, skipped_candidate_count
            try:
                url = canonicalize_nptu_url(candidate.url)
            except ValueError:
                skipped_candidate_count += 1
                return
            if (
                url in visited
                or not is_allowed_source_url(url, allowed_hosts)
                or not self._adapter.is_crawlable_url(url)
            ):
                skipped_candidate_count += 1
                return
            normalized_candidate = replace(candidate, url=url)
            relevance = self._scorer.score_candidate(search_plan, normalized_candidate)
            current_score = queued_scores.get(url)
            if current_score is not None:
                skipped_candidate_count += 1
                if current_score >= relevance:
                    return
            if (
                url not in discovered_urls
                and len(discovered_urls) >= self._config.max_candidate_urls
            ):
                skipped_candidate_count += 1
                return
            discovered_urls.add(url)
            queued_scores[url] = relevance
            heapq.heappush(
                queue,
                (-relevance, sequence, normalized_candidate),
            )
            sequence += 1

        site_map_candidates = ()
        site_map_sufficient = False
        if self._site_map is not None:
            map_started = self._clock()
            try:
                site_map_candidates = self._site_map.find_candidates(
                    search_plan,
                    scope=scope,
                    allowed_hosts=allowed_hosts,
                    limit=self._config.max_candidate_urls,
                )
                site_map_sufficient = self._site_map.has_sufficient_candidates(
                    site_map_candidates,
                    minimum=min(self._config.early_stop_min_results, limit),
                )
                logger.info(
                    "site map candidates loaded",
                    extra={
                        "site_map_candidate_count": len(site_map_candidates),
                        "site_map_candidate_query_ms": round(
                            (self._clock() - map_started) * 1000,
                            2,
                        ),
                    },
                )
            except Exception:
                logger.exception("site map candidate lookup 失敗")
        for item in site_map_candidates:
            enqueue(
                CandidatePage(
                    item.canonical_url,
                    anchor_text=item.title or "",
                    depth=item.minimum_depth,
                    discovery_relevance=item.relevance,
                )
            )
        for seed_url in seed_urls:
            enqueue(CandidatePage(seed_url))

        if (
            effective_discovery
            and self._discovery is not None
            and not site_map_sufficient
        ):
            try:
                search_deadline.raise_if_expired()
                for item in self._discovery.discover(
                    search_plan,
                    max_items=self._config.max_candidate_urls,
                    deadline=search_deadline,
                ):
                    enqueue(
                        CandidatePage(
                            item.url,
                            anchor_text=item.label,
                            discovery_relevance=item.relevance,
                        )
                    )
                    if self._site_map is not None:
                        try:
                            self._site_map.record_discovery(
                                item.url,
                                title=item.label,
                                unit=(
                                    scope.canonical_unit if scope is not None else None
                                ),
                            )
                        except Exception:
                            logger.exception("官方搜尋結果寫入 site map 失敗")
            except SearchDeadlineExceeded:
                query_timed_out = True
            except Exception:
                logger.exception("NPTU 官方網站 discovery 失敗")
        elif effective_discovery and site_map_sufficient:
            logger.info(
                "official search skipped due to site map",
                extra={
                    "official_search_skipped_due_to_site_map": True,
                    "site_map_candidate_count": len(site_map_candidates),
                },
            )

        while queue and len(visited) < self._config.max_pages:
            if search_deadline.expired():
                query_timed_out = True
                timed_out_scores.extend(queued_scores.values())
                break
            priority, _sequence, candidate = heapq.heappop(queue)
            relevance = -priority
            url = candidate.url
            if relevance < queued_scores.get(url, -1.0):
                continue
            queued_scores.pop(url, None)
            if url in visited:
                skipped_candidate_count += 1
                continue
            host = urlsplit(url).hostname or ""
            if pages_per_host.get(host, 0) >= self._config.max_pages_per_host:
                skipped_candidate_count += 1
                continue
            visited.add(url)
            pages_per_host[host] = pages_per_host.get(host, 0) + 1
            try:
                page = self.fetch_page(
                    url,
                    allowed_hosts=allowed_hosts,
                    deadline=search_deadline,
                )
            except SearchDeadlineExceeded:
                query_timed_out = True
                timed_out_scores.append(relevance)
                timed_out_scores.extend(queued_scores.values())
                break
            except Exception:
                fetch_failure_scores.append(relevance)
                if self._site_map is not None and not search_deadline.expired():
                    try:
                        self._site_map.record_crawl_failure(url)
                    except Exception:
                        logger.exception("site map crawl failure state 寫入失敗")
                logger.exception(
                    "NPTU 官方網站頁面取得失敗",
                    extra={"url": url},
                )
                continue

            pages.append(page)
            candidates.append(candidate)
            if self._site_map is not None:
                try:
                    self._site_map.record_fetched_page(
                        page,
                        unit=(scope.canonical_unit if scope is not None else None),
                        depth=candidate.depth,
                        allowed_hosts=allowed_hosts,
                    )
                except Exception:
                    logger.exception("site map page/link persistence 失敗")
            preliminary = self._scorer.score_candidate(
                search_plan,
                replace(
                    candidate,
                    anchor_text=" ".join(
                        (
                            candidate.anchor_text,
                            page.title,
                            " ".join(page.headings),
                            page.body[:2_000],
                        )
                    ),
                ),
            )
            preliminary_scores.append(preliminary)
            if candidate.depth < self._config.max_depth:
                link_details = page.link_texts or tuple(
                    (link, "") for link in page.links
                )
                for link, label in link_details:
                    enqueue(
                        CandidatePage(
                            link,
                            anchor_text=label,
                            depth=candidate.depth + 1,
                            parent_relevance=preliminary,
                        )
                    )
            if (
                len(pages) >= self._config.early_stop_min_results
                and sum(
                    score >= self._config.high_confidence_score
                    for score in preliminary_scores
                )
                >= self._config.early_stop_min_results
                and (not queue or -queue[0][0] < self._config.relevance_threshold)
            ):
                break

        if queue and not query_timed_out:
            skipped_candidate_count += len(queued_scores)
        try:
            search_deadline.raise_if_expired()
            scores = self._scorer.score_pages(
                search_plan,
                candidates,
                pages,
                deadline=search_deadline,
                execution_context=execution_context,
            )
            search_deadline.raise_if_expired()
        except SearchDeadlineExceeded:
            query_timed_out = True
            scores = preliminary_scores
        scored_pages = []
        for page, score in zip(pages, scores, strict=True):
            if scope is not None:
                host = (urlsplit(page.canonical_url).hostname or "").lower()
                if host in scope.preferred_hosts:
                    score = min(1.0, score + 0.14)
            scored_pages.append(replace(page, score=score))
        relevant_pages = [
            page
            for page in scored_pages
            if page.score >= self._config.relevance_threshold
            or (
                scope is not None
                and page.role is UnitAnnouncementPageRole.LISTING
                and page.announcement_items
            )
        ]
        relevant_pages.sort(
            key=lambda page: (
                page.score,
                page.published_at is not None,
                page.published_at or date.min,
            ),
            reverse=True,
        )
        relevant_failed_scores = [
            score
            for score in fetch_failure_scores
            if score >= self._config.relevance_threshold
        ]
        unrelated_failed_scores = [
            score
            for score in fetch_failure_scores
            if score < self._config.relevance_threshold
        ]
        relevant_success_scores = [page.score for page in relevant_pages]
        diagnostics = SearchDiagnostics(
            discovered_count=len(discovered_urls),
            fetched_count=len(pages),
            relevant_success_count=len(relevant_pages),
            relevant_fetch_failure_count=len(relevant_failed_scores),
            unrelated_fetch_failure_count=len(unrelated_failed_scores),
            timed_out_candidate_count=len(timed_out_scores),
            skipped_candidate_count=skipped_candidate_count,
            query_timed_out=query_timed_out,
            highest_success_score=max(relevant_success_scores, default=None),
            highest_fetch_failure_score=max(relevant_failed_scores, default=None),
            highest_unattempted_score=max(timed_out_scores, default=None),
        )
        result = SiteSearchResult(tuple(relevant_pages[:limit]), diagnostics)
        return result


class DocumentRepository(Protocol):
    def has_hash(self, canonical_url: str, digest: str) -> bool: ...

    def save(
        self,
        metadata: DocumentMetadata,
        raw_text: str,
        chunks: list[TextChunk],
        embeddings: list[list[float]],
    ) -> None: ...


class ScopedAnnouncementRepository(Protocol):
    def merge_source_announcements(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
    ) -> list[str]: ...


class ScoredEvidence(Protocol):
    @property
    def score(self) -> float: ...

    @property
    def content(self) -> str: ...


@dataclass(frozen=True, slots=True)
class SitePageIngestionResult:
    summary: IngestionSummary
    warning: str | None
    diagnostics: SearchDiagnostics = SearchDiagnostics()
    deadline: SearchDeadline | None = None
    relevant_pages_found: int = 0
    relevant_pages_persisted: int = 0
    ingestion_timed_out: bool = False
    ingestion_complete: bool = True


@dataclass(frozen=True, slots=True)
class ScopedAnnouncementIngestionResult:
    canonical_urls: tuple[str, ...]
    warning: str | None
    diagnostics: SearchDiagnostics = SearchDiagnostics()
    found_count: int = 0
    persisted_count: int = 0
    failed_count: int = 0
    undated_count: int = 0
    complete: bool = True


class SitePageIngestionService:
    def __init__(
        self,
        search_service: NptuSiteSearchService,
        repository: DocumentRepository,
        embedding_provider: EmbeddingProvider,
        config: SiteSearchConfig,
        announcement_repository: ScopedAnnouncementRepository | None = None,
    ) -> None:
        self._search = search_service
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._config = config
        self._announcement_repository = announcement_repository

    def should_search_live(self, evidence: Collection[ScoredEvidence]) -> bool:
        reliable = [
            item
            for item in evidence
            if item.score >= self._config.database_min_score
            and len(item.content.strip()) >= self._config.database_min_content_chars
        ]
        return len(reliable) < self._config.database_min_results

    def new_deadline(self) -> SearchDeadline:
        return self._search.new_deadline()

    def ingest(
        self,
        plan: SearchPlan | str,
        *,
        max_items: int,
        deadline: SearchDeadline | None = None,
        scope: DocumentSearchScope | None = None,
        execution_context: RetrievalExecutionContext | None = None,
    ) -> SitePageIngestionResult:
        deadline = deadline or self.new_deadline()
        search_result = self._search.search(
            plan,
            max_items=max_items,
            deadline=deadline,
            scope=scope,
            execution_context=execution_context,
        )
        diagnostics = search_result.diagnostics
        summary = IngestionSummary()
        relevant_pages_found = len(search_result.pages)
        relevant_pages_persisted = 0

        def result(
            warning: str | None,
            *,
            timed_out: bool = False,
        ) -> SitePageIngestionResult:
            return SitePageIngestionResult(
                summary=summary,
                warning=warning,
                diagnostics=diagnostics,
                deadline=deadline,
                relevant_pages_found=relevant_pages_found,
                relevant_pages_persisted=relevant_pages_persisted,
                ingestion_timed_out=timed_out,
                ingestion_complete=(
                    relevant_pages_persisted == relevant_pages_found
                    and summary.failed == 0
                ),
            )

        prepared: list[tuple[NptuSitePage, str, str, list[TextChunk]]] = []
        for page in search_result.pages:
            try:
                deadline.raise_if_expired()
                raw_text = page.body.strip()
                if not raw_text:
                    summary.skipped += 1
                    continue
                digest = content_hash(raw_text)
                already_stored = self._repository.has_hash(page.canonical_url, digest)
                deadline.raise_if_expired()
                if already_stored:
                    summary.skipped += 1
                    relevant_pages_persisted += 1
                    continue
                chunks = chunk_text(raw_text)
                deadline.raise_if_expired()
                prepared.append((page, raw_text, digest, chunks))
            except SearchDeadlineExceeded:
                diagnostics = replace(diagnostics, query_timed_out=True)
                return result(
                    self._warning_for(diagnostics),
                    timed_out=True,
                )
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{page.canonical_url}: {type(exc).__name__}")

        chunk_texts = [
            chunk.content
            for _page, _raw_text, _digest, chunks in prepared
            for chunk in chunks
        ]
        all_embeddings: list[list[float]] = []
        try:
            for start in range(0, len(chunk_texts), self._config.embedding_batch_size):
                deadline.raise_if_expired()
                all_embeddings.extend(
                    self._embedding_provider.embed(
                        chunk_texts[start : start + self._config.embedding_batch_size],
                        timeout_seconds=deadline.remaining_seconds(),
                    )
                )
                deadline.raise_if_expired()
            deadline.raise_if_expired()
        except SearchDeadlineExceeded:
            diagnostics = replace(diagnostics, query_timed_out=True)
            return result(
                self._warning_for(diagnostics),
                timed_out=True,
            )
        except Exception as exc:
            if deadline.expired():
                diagnostics = replace(diagnostics, query_timed_out=True)
                return result(
                    self._warning_for(diagnostics),
                    timed_out=True,
                )
            summary.failed += len(prepared)
            summary.errors.extend(
                f"{page.canonical_url}: {type(exc).__name__}"
                for page, _raw_text, _digest, _chunks in prepared
            )
            return result(SITE_SEARCH_FAILURE_WARNING)

        embedding_offset = 0
        for page, raw_text, digest, chunks in prepared:
            try:
                deadline.raise_if_expired()
                embeddings = all_embeddings[
                    embedding_offset : embedding_offset + len(chunks)
                ]
                embedding_offset += len(chunks)
                if len(embeddings) != len(chunks):
                    raise ValueError("頁面分塊與 embedding 數量不一致")
                metadata = DocumentMetadata(
                    title=page.title,
                    source_url=HttpUrl(page.canonical_url),
                    unit=(
                        scope.canonical_unit
                        if scope is not None and scope.canonical_unit is not None
                        else self._config.unit
                    ),
                    published_at=page.published_at,
                    effective_from=page.published_at or date.today(),
                    document_type="official_web_page",
                    version=digest[:12],
                )
                self._repository.save(metadata, raw_text, chunks, embeddings)
                summary.created += 1
                relevant_pages_persisted += 1
                deadline.raise_if_expired()
            except SearchDeadlineExceeded:
                diagnostics = replace(diagnostics, query_timed_out=True)
                break
            except Exception as exc:
                summary.failed += 1
                summary.errors.append(f"{page.canonical_url}: {type(exc).__name__}")

        warning = self._warning_for(diagnostics)
        if summary.failed:
            warning = (
                SITE_SEARCH_FAILURE_WARNING
                if summary.created == 0 and summary.skipped == 0
                else SITE_SEARCH_PARTIAL_WARNING
            )
        return result(warning, timed_out=diagnostics.query_timed_out)

    def search_unit_announcements(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope,
        max_items: int,
        deadline: SearchDeadline,
        sort: object = "newest",
        topic: str | None = None,
    ) -> ScopedAnnouncementIngestionResult:
        search_limit = min(self._config.max_items, max(max_items, 3))
        result = self._search.search(
            plan,
            max_items=search_limit,
            use_discovery=False,
            deadline=deadline,
            scope=scope,
        )
        canonical_unit = scope.canonical_unit
        if not canonical_unit:
            raise ValueError("scoped 公告搜尋缺少 canonical unit")
        if self._announcement_repository is None:
            return ScopedAnnouncementIngestionResult(
                (),
                SITE_SEARCH_FAILURE_WARNING,
                result.diagnostics,
                complete=False,
            )

        listing_items: list[NptuListingItem] = []
        detail_pages: dict[str, tuple[NptuSitePage, NptuListingItem | None]] = {}
        for page in result.pages:
            if page.role is UnitAnnouncementPageRole.LISTING:
                listing_items.extend(page.announcement_items)
            elif page.role is UnitAnnouncementPageRole.DETAIL:
                detail_pages[page.canonical_url] = (page, None)

        deduplicated_items = {item.canonical_url: item for item in listing_items}
        failed_count = 0
        timed_out = False
        for item in list(deduplicated_items.values())[:max_items]:
            if item.canonical_url in detail_pages:
                page, _existing = detail_pages[item.canonical_url]
                detail_pages[item.canonical_url] = (page, item)
                continue
            try:
                page = self._search.fetch_page(
                    item.canonical_url,
                    allowed_hosts=scope.allowed_hosts,
                    deadline=deadline,
                )
                if page.role is UnitAnnouncementPageRole.LISTING:
                    failed_count += 1
                    continue
                detail_pages[item.canonical_url] = (page, item)
            except SearchDeadlineExceeded:
                timed_out = True
                break
            except Exception:
                failed_count += 1
                logger.exception(
                    "單位公告詳情頁取得失敗",
                    extra={"unit": canonical_unit, "url": item.canonical_url},
                )

        candidates: list[AnnouncementCandidate] = []
        undated_count = 0
        for page, listing_item in detail_pages.values():
            published_at = page.published_at or (
                listing_item.published_at if listing_item is not None else None
            )
            if published_at is None:
                undated_count += 1
                continue
            title = page.title
            if listing_item is not None and (
                not title or title == page.canonical_url or len(title) > 300
            ):
                title = listing_item.title
            body = page.body.strip()
            if not body:
                failed_count += 1
                continue
            candidates.append(
                AnnouncementCandidate(
                    title=title,
                    canonical_url=page.canonical_url,
                    unit=canonical_unit,
                    category="單位公告",
                    published_at=published_at,
                    deadline_at=None,
                    body=body,
                )
            )

        sort_value = str(getattr(sort, "value", sort)).casefold()
        topic_key = re.sub(r"\s+", "", topic or "").casefold()

        def relevance(candidate: AnnouncementCandidate) -> int:
            if not topic_key:
                return 0
            searchable = re.sub(
                r"\s+", "", f"{candidate.title} {candidate.body}"
            ).casefold()
            return searchable.count(topic_key)

        if sort_value == "oldest":
            candidates.sort(
                key=lambda item: (item.published_at.toordinal(), item.canonical_url)
            )
        elif sort_value == "relevance":
            candidates.sort(
                key=lambda item: (
                    -relevance(item),
                    -item.published_at.toordinal(),
                    item.canonical_url,
                )
            )
        else:
            candidates.sort(
                key=lambda item: (-item.published_at.toordinal(), item.canonical_url)
            )

        source_url = scope.homepage_url or (
            scope.seed_urls[0] if scope.seed_urls else ""
        )
        source_name = f"unit-scoped:{canonical_unit}"
        persisted_urls: list[str] = []
        crawled_at = datetime.now(timezone.utc)
        for candidate in candidates[:max_items]:
            try:
                deadline.raise_if_expired()
                self._announcement_repository.merge_source_announcements(
                    [candidate],
                    source_name=source_name,
                    source_url=source_url,
                    source_unit=canonical_unit,
                    interval_minutes=60,
                    crawled_at=crawled_at,
                )
                persisted_urls.append(candidate.canonical_url)
            except SearchDeadlineExceeded:
                timed_out = True
                break
            except Exception:
                failed_count += 1
                logger.exception(
                    "單位公告持久化失敗",
                    extra={"unit": canonical_unit, "url": candidate.canonical_url},
                )

        found_count = len(detail_pages)
        incomplete = bool(
            failed_count
            or undated_count
            or timed_out
            or result.diagnostics.query_timed_out
            or self._warning_for(result.diagnostics)
        )
        if persisted_urls:
            warning = SITE_SEARCH_PARTIAL_WARNING if incomplete else None
        elif incomplete:
            warning = SITE_SEARCH_FAILURE_WARNING
        else:
            warning = None
        return ScopedAnnouncementIngestionResult(
            canonical_urls=tuple(dict.fromkeys(persisted_urls)),
            warning=warning,
            diagnostics=result.diagnostics,
            found_count=found_count,
            persisted_count=len(persisted_urls),
            failed_count=failed_count,
            undated_count=undated_count,
            complete=not incomplete,
        )

    def _warning_for(self, diagnostics: SearchDiagnostics) -> str | None:
        if diagnostics.relevant_success_count == 0:
            return (
                SITE_SEARCH_FAILURE_WARNING
                if diagnostics.relevant_fetch_failure_count
                or diagnostics.query_timed_out
                else None
            )
        highest_success = diagnostics.highest_success_score or 0.0
        success_is_sufficient = (
            diagnostics.relevant_success_count >= self._config.early_stop_min_results
            and highest_success >= self._config.high_confidence_score
        )
        if success_is_sufficient:
            return None
        highest_failed = diagnostics.highest_fetch_failure_score
        if (
            highest_failed is not None
            and highest_failed > highest_success + self._config.failure_warning_margin
        ):
            return SITE_SEARCH_PARTIAL_WARNING
        highest_unattempted = diagnostics.highest_unattempted_score
        if (
            diagnostics.query_timed_out
            and highest_unattempted is not None
            and highest_unattempted
            > highest_success + self._config.failure_warning_margin
        ):
            return SITE_SEARCH_PARTIAL_WARNING
        return None


def site_page_to_announcement_result(
    page: NptuSitePage,
    *,
    config: SiteSearchConfig,
) -> AnnouncementSearchResult:
    return AnnouncementSearchResult(
        title=page.title,
        canonical_url=page.canonical_url,
        unit=config.unit,
        category=config.category,
        published_at=page.published_at,
        body=page.body,
        source_name=config.name,
        source_url=config.seed_urls[0],
    )
