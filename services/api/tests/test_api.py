from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from nptu_assistant.api.schemas import (
    AnnouncementListResponse,
    AnswerType,
    ChatResponse,
    Confidence,
    CrawlSummary,
    IngestionSummary,
    SiteMapSyncResponse,
)
from nptu_assistant.core.settings import Settings
from nptu_assistant.main import create_app


class StubHealth:
    def check(self) -> dict[str, object]:
        return {
            "status": "degraded",
            "checks": {
                "database": "ok",
                "llm": "not_configured",
                "embeddings": "not_configured",
            },
        }


class StubChat:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def answer(self, question: str, conversation_id: str | None = None) -> ChatResponse:
        return ChatResponse(
            conversation_id=conversation_id or "conversation-new",
            answer=f"測試回答：{question}",
            answer_type=AnswerType.INSUFFICIENT_INFORMATION,
            confidence=Confidence.LOW,
            sources=[],
            warning="目前收錄的官方資料不足以確認",
        )

    def delete_conversation(self, conversation_id: str) -> bool:
        self.deleted.append(conversation_id)
        return True


class FailingChat:
    def answer(self, question: str, conversation_id: str | None = None) -> ChatResponse:
        del question, conversation_id
        raise RuntimeError("do not expose this internal message")

    def delete_conversation(self, conversation_id: str) -> bool:
        del conversation_id
        raise RuntimeError("do not expose this internal message")


class DenyLimiter:
    def allow(self, bucket: str, key: str, *, limit: int, window_seconds: int) -> bool:
        del bucket, key, limit, window_seconds
        return False


class StubAnnouncements:
    def list(self, **kwargs: object) -> AnnouncementListResponse:
        return AnnouncementListResponse(items=[], page=1, page_size=20, total=0)


class StubOperation:
    def run(self, source_names: list[str] | None = None) -> IngestionSummary | CrawlSummary:
        if source_names is None:
            return IngestionSummary(created=1)
        return CrawlSummary(created=1)


class StubSiteMap:
    def sync(self) -> SiteMapSyncResponse:
        return SiteMapSyncResponse(seen=2, created=2)


class StubScheduler:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.started = True
        await self._stop.wait()

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()


def make_client(
    *,
    chat_service=None,
    site_map_service=None,
    rate_limiter=None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    settings = Settings(
        _env_file=None,
        admin_api_enabled=True,
        admin_api_key="test-admin-key",
        cors_allowed_origins="http://localhost:3000",
        openai_api_key=None,
    )
    app = create_app(
        settings=settings,
        health_service=StubHealth(),
        chat_service=chat_service or StubChat(),
        announcement_service=StubAnnouncements(),
        ingestion_service=StubOperation(),
        crawler_service=StubOperation(),
        site_map_service=site_map_service or StubSiteMap(),
        rate_limiter=rate_limiter,
    )
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_app_lifespan_starts_and_stops_refresh_scheduler() -> None:
    scheduler = StubScheduler()
    settings = Settings(
        _env_file=None,
        admin_api_enabled=True,
        admin_api_key="test-admin-key",
        cors_allowed_origins="http://localhost:3000",
        openai_api_key=None,
    )
    app = create_app(
        settings=settings,
        health_service=StubHealth(),
        chat_service=StubChat(),
        announcement_service=StubAnnouncements(),
        ingestion_service=StubOperation(),
        crawler_service=StubOperation(),
        refresh_scheduler=scheduler,
    )

    with TestClient(app):
        assert scheduler.started is True

    assert scheduler.stopped is True


def test_health_returns_degraded_without_llm() -> None:
    response = make_client().get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


def test_chat_validates_and_returns_schema() -> None:
    client = make_client()

    response = client.post("/v1/chat", json={"question": "  測試問題  "})
    invalid = client.post("/v1/chat", json={"question": " "})

    assert response.status_code == 200
    assert response.json()["conversation_id"] == "conversation-new"
    assert response.json()["answer"].endswith("測試問題")
    assert invalid.status_code == 422
    assert "error" in invalid.json()


def test_chat_accepts_existing_conversation_and_delete_clears_server_state() -> None:
    chat = StubChat()
    client = make_client(chat_service=chat)

    response = client.post(
        "/v1/chat",
        json={"question": "第三則", "conversation_id": "conversation-existing"},
    )
    deleted = client.delete("/v1/conversations/conversation-existing")

    assert response.status_code == 200
    assert response.json()["conversation_id"] == "conversation-existing"
    assert deleted.status_code == 204
    assert chat.deleted == ["conversation-existing"]


def test_announcements_have_paginated_shape() -> None:
    response = make_client().get("/v1/announcements?page=1&page_size=20")

    assert response.status_code == 200
    assert response.json() == {"items": [], "page": 1, "page_size": 20, "total": 0}


def test_admin_endpoint_requires_key() -> None:
    client = make_client()

    denied = client.post("/v1/admin/ingest/documents")
    allowed = client.post(
        "/v1/admin/ingest/documents", headers={"X-Admin-Key": "test-admin-key"}
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "admin_unauthorized"
    assert allowed.status_code == 200
    assert allowed.json()["created"] == 1
    assert set(allowed.json()) == {"created", "skipped", "failed", "errors"}


def test_crawl_endpoint_uses_crawl_summary_contract() -> None:
    response = make_client().post(
        "/v1/admin/crawl/announcements",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"source_names": ["nptu-overview"]},
    )

    assert response.status_code == 200
    assert set(response.json()) == {"created", "updated", "unchanged", "failed", "errors"}


def test_site_map_sync_endpoint_uses_admin_auth_and_summary_contract() -> None:
    client = make_client()

    denied = client.post("/v1/admin/site-map/sync")
    allowed = client.post(
        "/v1/admin/site-map/sync",
        headers={"X-Admin-Key": "test-admin-key"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json() == {
        "seen": 2,
        "created": 2,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "links_created": 0,
    }


def test_cors_only_allows_configured_origin() -> None:
    client = make_client()

    allowed = client.options(
        "/v1/chat",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    denied = client.options(
        "/v1/chat",
        headers={"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
    )

    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in denied.headers


def test_rate_limiter_is_replaceable() -> None:
    response = make_client(rate_limiter=DenyLimiter()).post(
        "/v1/chat", json={"question": "測試"}
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


def test_http_and_unexpected_errors_use_error_envelope() -> None:
    client = make_client(chat_service=FailingChat(), raise_server_exceptions=False)

    missing = client.get("/missing")
    failed = client.post("/v1/chat", json={"question": "測試"})

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "internal_error"
    assert "do not expose" not in failed.text


def test_request_id_only_accepts_safe_header_characters() -> None:
    client = make_client()

    accepted = client.get("/health", headers={"X-Request-ID": "request-123"})
    replaced = client.get("/health", headers={"X-Request-ID": "unsafe request/id"})

    assert accepted.headers["X-Request-ID"] == "request-123"
    assert replaced.headers["X-Request-ID"] != "unsafe request/id"


def test_chat_reports_provider_unavailable_without_openai_key() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key=None,
        llm_provider="openai",
        embedding_provider="openai",
    )
    app = create_app(
        settings=settings,
        health_service=StubHealth(),
        chat_service=None,
        announcement_service=StubAnnouncements(),
        ingestion_service=StubOperation(),
        crawler_service=StubOperation(),
    )

    response = TestClient(app).post("/v1/chat", json={"question": "測試"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


def test_openapi_documents_the_unified_error_envelope() -> None:
    client = make_client()
    schema = client.app.openapi()

    error_schema = schema["paths"]["/v1/chat"]["post"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"]

    assert error_schema["$ref"].endswith("/ErrorResponse")


def test_openapi_keeps_question_compatible_and_adds_conversation_source_identity() -> None:
    schema = make_client().app.openapi()["components"]["schemas"]

    assert schema["ChatRequest"]["required"] == ["question"]
    assert "conversation_id" in schema["ChatRequest"]["properties"]
    assert "conversation_id" in schema["ChatResponse"]["required"]
    assert {"id", "kind", "title", "url"} <= set(schema["SourceReference"]["required"])
