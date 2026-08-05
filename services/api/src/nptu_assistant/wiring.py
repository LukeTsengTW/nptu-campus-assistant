from __future__ import annotations

from typing import cast

from openai import OpenAI

from nptu_assistant.api.crawl_admin import CrawlWorkerController
from nptu_assistant.api.errors import AppError
from nptu_assistant.api.services import AnnouncementService, HealthService
from nptu_assistant.core.settings import (
    WORKSPACE_ROOT,
    Settings,
    resolve_workspace_path,
)
from nptu_assistant.crawlers.config import (
    SiteSearchConfig,
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.announcement_adapter import (
    AnnouncementSourceIdentity,
    IncrementalAnnouncementAdapter,
)
from nptu_assistant.crawlers.crawl_ingestion import CrawlIngestionService
from nptu_assistant.crawlers.crawl_scheduler import (
    CrawlScheduler,
    canonical_crawl_identity,
)
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.incremental_crawler import (
    IncrementalCrawler,
    IncrementalCrawlScheduler,
)
from nptu_assistant.crawlers.official_units import (
    load_official_unit_directory_for_config,
)
from nptu_assistant.crawlers.refresh import (
    AnnouncementRefreshCoordinator,
    AnnouncementRefreshScheduler,
)
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.crawlers.search import KeywordAnnouncementSearchService
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.crawlers.site_discovery import NptuOfficialSearchDiscovery
from nptu_assistant.crawlers.site_map import SiteMapService
from nptu_assistant.crawlers.site_models import (
    ProgressiveRetrievalPolicy,
    SearchExecutionLimits,
)
from nptu_assistant.crawlers.site_scoring import HybridCandidateScorer
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SitePageIngestionService,
)
from nptu_assistant.crawlers.site_search_cache import (
    InMemorySiteSearchCache,
    LayeredSiteSearchCache,
    PostgresSiteSearchCache,
    SingleFlightSearchRunner,
)
from nptu_assistant.db.crawl_scheduler import SqlCrawlSchedulerRepository
from nptu_assistant.db.repositories import (
    SqlAnnouncementRepository,
    SqlDocumentRepository,
)
from nptu_assistant.db.session import create_session_factory
from nptu_assistant.db.site_map import SqlSiteMapRepository
from nptu_assistant.ingestion.service import DocumentIngestionService
from nptu_assistant.providers.fake import FakeEmbeddingProvider, FakeLlmProvider
from nptu_assistant.providers.openai import OpenAIEmbeddingProvider, OpenAILlmProvider
from nptu_assistant.providers.protocols import EmbeddingProvider
from nptu_assistant.rag.announcement_dedup import DeduplicatingSqlRetriever
from nptu_assistant.rag.conversation import SqlConversationStore
from nptu_assistant.rag.completeness import (
    CompletenessConfig,
    CompletenessMode,
    DbFirstCompletenessPolicy,
)
from nptu_assistant.rag.completeness_facts import SqlRetrievalCompletenessFacts
from nptu_assistant.rag.completeness_refresh import CompletenessRefreshScheduler
from nptu_assistant.rag.service import ChatService, LlmProvider
from nptu_assistant.core.security import is_allowed_source_url
from collections.abc import Sequence
from urllib.parse import urlsplit


class UnavailableEmbeddingProvider:
    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del texts, timeout_seconds
        raise AppError(
            "embedding_provider_unavailable",
            "目前未設定向量服務。",
            status_code=503,
        )


class _IncrementalWorkerCoordinator:
    """將頁面 worker 接到既有 admin control 的最小介面。"""

    def __init__(self, worker: IncrementalCrawler) -> None:
        self._worker = worker

    def refresh_due_sources(self) -> list[object]:
        return [self._worker.run_once()]

    def ensure_fresh(self, source_name: str) -> object:
        del source_name
        return self._worker.run_once()


def _resolve_incremental_announcement_source(
    page_url: str,
    unit: str | None,
    source_configs: Sequence[object],
) -> AnnouncementSourceIdentity | None:
    """Resolve announcement identity only from configured allowlist data."""
    if not unit:
        return None
    host = (urlsplit(page_url).hostname or "").casefold().rstrip(".")
    matches = []
    for config in source_configs:
        if (
            not getattr(config, "enabled", False)
            or getattr(config, "unit", None) != unit
        ):
            continue
        allowed_hosts = tuple(getattr(config, "allowed_hosts", ()) or ())
        if not is_allowed_source_url(page_url, allowed_hosts):
            continue
        config_host = (
            (urlsplit(getattr(config, "url", "")).hostname or "").casefold().rstrip(".")
        )
        matches.append((config_host == host, str(getattr(config, "name", "")), config))
    if not matches:
        return None
    _exact_host, _name, config = sorted(
        matches, key=lambda item: (not item[0], item[1])
    )[0]
    return AnnouncementSourceIdentity(
        name=config.name,
        url=config.url,
        unit=config.unit,
        interval_minutes=config.crawl_interval_minutes,
    )


def build_services(settings: Settings) -> dict[str, object]:
    factory = create_session_factory(settings)
    crawler_config_path = resolve_workspace_path(settings.crawler_config_path)
    source_configs = load_source_configs(crawler_config_path)
    keyword_search_config = load_keyword_search_config(crawler_config_path)
    official_units = load_official_unit_directory_for_config(crawler_config_path)
    openai_api_key = settings.openai_api_key
    openai_client = (
        OpenAI(api_key=openai_api_key.get_secret_value())
        if openai_api_key is not None
        and (
            settings.embedding_provider == "openai" or settings.llm_provider == "openai"
        )
        else None
    )
    embedding: EmbeddingProvider
    if settings.embedding_provider == "fake":
        embedding = FakeEmbeddingProvider(settings.openai_embedding_dimensions)
    elif openai_client is not None:
        embedding = OpenAIEmbeddingProvider(
            openai_client,
            settings.openai_embedding_model,
            settings.openai_embedding_dimensions,
        )
    else:
        embedding = UnavailableEmbeddingProvider()
    llm: LlmProvider | None
    if settings.llm_provider == "fake":
        llm = FakeLlmProvider(official_units)
    elif openai_client is not None:
        llm = OpenAILlmProvider(
            openai_client,
            settings.openai_text_model,
        )
    else:
        llm = None
    site_config = keyword_search_config.site_search
    document_repository = SqlDocumentRepository(factory)
    announcement_repository = SqlAnnouncementRepository(factory)
    site_map_repository = SqlSiteMapRepository(
        factory,
        site_map_query_budget_ratio=(
            site_config.site_map_query_budget_ratio if site_config else 0.25
        ),
        site_map_query_min_seconds=(
            site_config.site_map_query_min_seconds if site_config else 0.05
        ),
        site_map_query_max_seconds=(
            site_config.site_map_query_max_seconds if site_config else 0.75
        ),
    )
    site_map_service = SiteMapService(
        site_map_repository,
        official_units=official_units,
        source_configs=source_configs,
        site_config=site_config or SiteSearchConfig(),
    )
    progressive_policy = ProgressiveRetrievalPolicy(
        min_results=site_config.database_min_results if site_config else 2,
        min_score=site_config.database_min_score if site_config else 0.58,
        min_content_chars=(
            site_config.database_min_content_chars if site_config else 160
        ),
    )
    http_client = CrawlHttpClient(
        settings.crawler_user_agent,
        interval_seconds=settings.crawler_request_interval_seconds,
        max_response_bytes=(
            site_config.max_response_bytes if site_config else 2 * 1024 * 1024
        ),
        timeout_seconds=site_config.request_timeout_seconds if site_config else 15.0,
    )
    crawler_service = CrawlerService(
        crawler_config_path,
        announcement_repository,
        http_client,
        workspace_root=WORKSPACE_ROOT,
    )
    site_discovery = (
        NptuOfficialSearchDiscovery(keyword_search_config, site_config, http_client)
        if site_config and site_config.enabled
        else None
    )
    site_searcher = (
        NptuSiteSearchService(
            site_config,
            http_client,
            scorer=HybridCandidateScorer(
                site_config.weights,
                embedding,
                batch_size=site_config.embedding_batch_size,
            ),
            discovery=site_discovery,
            cache=LayeredSiteSearchCache(
                InMemorySiteSearchCache(),
                PostgresSiteSearchCache(factory),
                ttl_seconds=site_config.cache_ttl_seconds,
            ),
            single_flight=SingleFlightSearchRunner(factory),
            site_map=site_map_service,
        )
        if site_config and site_config.enabled
        else None
    )
    keyword_search_service = KeywordAnnouncementSearchService(
        keyword_search_config,
        announcement_repository,
        http_client,
        site_searcher=site_searcher,
    )
    site_page_ingestor = (
        SitePageIngestionService(
            site_searcher,
            document_repository,
            embedding,
            site_config,
            announcement_repository,
        )
        if site_searcher and site_config
        else None
    )
    crawl_ingestion_service = CrawlIngestionService(
        document_repository,
        embedding,
        default_unit="國立屏東大學",
        announcement_adapter=IncrementalAnnouncementAdapter(announcement_repository),
        announcement_source_resolver=lambda page, unit: (
            _resolve_incremental_announcement_source(
                page.canonical_url,
                unit,
                source_configs,
            )
        ),
    )
    crawl_lease_repository = SqlCrawlSchedulerRepository(factory)
    crawl_scheduler = CrawlScheduler(
        crawl_lease_repository,
        identity=canonical_crawl_identity,
    )
    completeness_settings = keyword_search_config.completeness_policy
    completeness_policy = DbFirstCompletenessPolicy(
        CompletenessConfig(
            min_strong_evidence=completeness_settings.min_strong_evidence,
            exact_scope_min_score=completeness_settings.exact_scope_min_score,
            minimum_score_margin=completeness_settings.minimum_score_margin,
            minimum_remaining_deadline_seconds=(
                completeness_settings.minimum_remaining_deadline_seconds
            ),
            minimum_source_coverage_ratio=(
                completeness_settings.minimum_source_coverage_ratio
            ),
            document_soft_stale_minutes=(
                completeness_settings.document_soft_stale_minutes
            ),
            document_hard_stale_minutes=(
                completeness_settings.document_hard_stale_minutes
            ),
            announcement_soft_stale_minutes=(
                completeness_settings.announcement_soft_stale_minutes
            ),
            announcement_hard_stale_minutes=(
                completeness_settings.announcement_hard_stale_minutes
            ),
            live_fallback_enabled=completeness_settings.live_fallback_enabled,
            live_fallback_max_details=(completeness_settings.live_fallback_max_details),
            rollout_mode=CompletenessMode(completeness_settings.rollout_mode),
        )
    )
    completeness_facts = SqlRetrievalCompletenessFacts(
        factory,
        announcement_source_targets=tuple(
            (config.name, config.url, config.unit)
            for config in source_configs
            if config.enabled
        ),
    )
    completeness_refresh_scheduler = CompletenessRefreshScheduler(
        crawl_scheduler,
        max_targets=completeness_settings.refresh_schedule_max_targets,
    )
    live_fallback_limits = SearchExecutionLimits(
        max_pages=completeness_settings.live_fallback_max_pages,
        max_candidate_urls=completeness_settings.live_fallback_max_candidate_urls,
        max_depth=completeness_settings.live_fallback_max_depth,
        max_pages_per_host=min(
            completeness_settings.live_fallback_max_pages,
            site_config.max_pages_per_host if site_config else 1,
        ),
    )
    incremental_crawler = IncrementalCrawler(
        http_client,
        site_map_service,
        scheduler=cast(IncrementalCrawlScheduler, crawl_scheduler),
        state_store=site_map_repository,
        ingestion_service=crawl_ingestion_service,
        allowed_hosts=("nptu.edu.tw",),
        host_interval_seconds=settings.crawler_request_interval_seconds,
        worker_id=f"api-{settings.app_host}:{settings.app_port}",
    )
    announcement_refresher = AnnouncementRefreshCoordinator(
        crawler_config_path,
        crawler_service,
        announcement_repository,
    )
    announcement_refresh_scheduler = AnnouncementRefreshScheduler(
        announcement_refresher
    )
    crawl_worker = CrawlWorkerController(
        _IncrementalWorkerCoordinator(incremental_crawler),
        interval_seconds=60.0,
        schedule_store=crawl_lease_repository,
    )
    return {
        "health_service": HealthService(factory, settings),
        "chat_service": (
            ChatService(
                DeduplicatingSqlRetriever(
                    factory,
                    embedding,
                    progressive_policy=progressive_policy,
                ),
                llm,
                SqlConversationStore(factory),
                announcement_refresher,
                keyword_search_service,
                UnitSourceResolver(
                    source_configs,
                    keyword_search_config.aliases,
                    keyword_search_config.source_routes,
                    official_units,
                ),
                site_page_ingestor,
                completeness_policy,
                completeness_facts,
                completeness_refresh_scheduler,
                live_fallback_limits,
                completeness_settings.live_fallback_max_seconds,
            )
            if llm
            else None
        ),
        "announcement_service": AnnouncementService(announcement_repository),
        "ingestion_service": DocumentIngestionService(
            resolve_workspace_path(settings.official_documents_path),
            document_repository,
            embedding,
        ),
        "crawl_ingestion_service": crawl_ingestion_service,
        "crawler_service": crawler_service,
        "refresh_scheduler": announcement_refresh_scheduler,
        "crawl_worker": crawl_worker,
        "crawl_admin_service": crawl_worker,
        "crawl_scheduler": crawl_scheduler,
        "crawl_lease_repository": crawl_lease_repository,
        "completeness_policy": completeness_policy,
        "incremental_crawler": incremental_crawler,
        "site_map_service": site_map_service,
        "session_factory": factory,
    }
