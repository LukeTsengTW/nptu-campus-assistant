from __future__ import annotations

from openai import OpenAI

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.services import AnnouncementService, HealthService
from nptu_assistant.core.settings import (
    Settings,
    WORKSPACE_ROOT,
    resolve_workspace_path,
)
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.refresh import (
    AnnouncementRefreshCoordinator,
    AnnouncementRefreshScheduler,
)
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.crawlers.search import KeywordAnnouncementSearchService
from nptu_assistant.crawlers.site_discovery import NptuOfficialSearchDiscovery
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SitePageIngestionService,
)
from nptu_assistant.crawlers.site_scoring import HybridCandidateScorer
from nptu_assistant.db.repositories import (
    SqlAnnouncementRepository,
    SqlDocumentRepository,
)
from nptu_assistant.db.session import create_session_factory
from nptu_assistant.ingestion.service import DocumentIngestionService
from nptu_assistant.providers.fake import FakeEmbeddingProvider, FakeLlmProvider
from nptu_assistant.providers.openai import OpenAIEmbeddingProvider, OpenAILlmProvider
from nptu_assistant.providers.protocols import EmbeddingProvider
from nptu_assistant.rag.conversation import SqlConversationStore
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.service import ChatService, LlmProvider


class UnavailableEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise AppError(
            "embedding_provider_unavailable",
            "目前未設定向量服務。",
            status_code=503,
        )


def build_services(settings: Settings) -> dict[str, object]:
    factory = create_session_factory(settings)
    crawler_config_path = resolve_workspace_path(settings.crawler_config_path)
    source_configs = load_source_configs(crawler_config_path)
    keyword_search_config = load_keyword_search_config(crawler_config_path)
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
        llm = FakeLlmProvider()
    elif openai_client is not None:
        llm = OpenAILlmProvider(
            openai_client,
            settings.openai_text_model,
        )
    else:
        llm = None
    document_repository = SqlDocumentRepository(factory)
    announcement_repository = SqlAnnouncementRepository(factory)
    site_config = keyword_search_config.site_search
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
        )
        if site_searcher and site_config
        else None
    )
    announcement_refresher = AnnouncementRefreshCoordinator(
        crawler_config_path,
        crawler_service,
        announcement_repository,
    )
    refresh_scheduler = AnnouncementRefreshScheduler(announcement_refresher)
    return {
        "health_service": HealthService(factory, settings),
        "chat_service": (
            ChatService(
                SqlRetriever(factory, embedding),
                llm,
                SqlConversationStore(factory),
                announcement_refresher,
                keyword_search_service,
                UnitSourceResolver(
                    source_configs,
                    keyword_search_config.aliases,
                    keyword_search_config.source_routes,
                ),
                site_page_ingestor,
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
        "crawler_service": crawler_service,
        "refresh_scheduler": refresh_scheduler,
        "session_factory": factory,
    }
