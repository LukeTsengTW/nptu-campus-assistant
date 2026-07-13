from __future__ import annotations

from openai import OpenAI

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.services import AnnouncementService, HealthService
from nptu_assistant.core.settings import Settings, WORKSPACE_ROOT, resolve_workspace_path
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.refresh import (
    AnnouncementRefreshCoordinator,
    AnnouncementRefreshScheduler,
)
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.db.repositories import SqlAnnouncementRepository, SqlDocumentRepository
from nptu_assistant.db.session import create_session_factory
from nptu_assistant.ingestion.service import DocumentIngestionService
from nptu_assistant.providers.fake import FakeEmbeddingProvider, FakeLlmProvider
from nptu_assistant.providers.openai import OpenAIEmbeddingProvider, OpenAILlmProvider
from nptu_assistant.rag.conversation import SqlConversationStore
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.service import ChatService


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
    openai_client = (
        OpenAI(api_key=settings.openai_api_key.get_secret_value())
        if settings.has_openai_key
        and (
            settings.embedding_provider == "openai"
            or settings.llm_provider == "openai"
        )
        else None
    )
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
    http_client = CrawlHttpClient(
        settings.crawler_user_agent,
        interval_seconds=settings.crawler_request_interval_seconds,
    )
    crawler_service = CrawlerService(
        resolve_workspace_path(settings.crawler_config_path),
        announcement_repository,
        http_client,
        workspace_root=WORKSPACE_ROOT,
    )
    announcement_refresher = AnnouncementRefreshCoordinator(
        resolve_workspace_path(settings.crawler_config_path),
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
