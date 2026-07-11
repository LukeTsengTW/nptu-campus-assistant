from __future__ import annotations

import uuid
import logging
import re
from datetime import date
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.schemas import (
    AnnouncementListResponse,
    ChatRequest,
    ChatResponse,
    CrawlSummary,
    CrawlRequest,
    ErrorResponse,
    IngestionSummary,
)
from nptu_assistant.core.logging import configure_logging
from nptu_assistant.core.rate_limit import InMemoryRateLimiter, RateLimiter
from nptu_assistant.core.security import secrets_match
from nptu_assistant.core.settings import Settings, get_settings


logger = logging.getLogger(__name__)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def _error_responses(*status_codes: int) -> dict[int, dict[str, object]]:
    return {
        status_code: {"model": ErrorResponse, "description": "統一錯誤 envelope"}
        for status_code in status_codes
    }


class _UnavailableChat:
    def answer(self, question: str) -> ChatResponse:
        raise AppError(
            "provider_unavailable",
            "目前未設定回答服務，請檢查後端 Provider 設定。",
            status_code=503,
        )


class _EmptyAnnouncements:
    def list(self, **kwargs: object) -> AnnouncementListResponse:
        return AnnouncementListResponse(
            items=[],
            page=int(kwargs.get("page", 1)),
            page_size=int(kwargs.get("page_size", 20)),
            total=0,
        )


class _UnavailableOperation:
    def run(self, source_names: list[str] | None = None) -> IngestionSummary | CrawlSummary:
        raise AppError("service_unavailable", "管理服務尚未初始化。", status_code=503)


class _DefaultHealth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self) -> dict[str, object]:
        llm = "configured" if self.settings.is_llm_configured else "not_configured"
        embeddings = "configured" if self.settings.is_embedding_configured else "not_configured"
        status = "ok" if llm == "configured" and embeddings == "configured" else "degraded"
        return {
            "status": status,
            "checks": {
                "database": "unknown",
                "llm": llm,
                "embeddings": embeddings,
            },
        }


def create_app(
    *,
    settings: Settings | None = None,
    health_service: Any | None = None,
    chat_service: Any | None = None,
    announcement_service: Any | None = None,
    ingestion_service: Any | None = None,
    crawler_service: Any | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    if any(
        service is None
        for service in (
            health_service,
            chat_service,
            announcement_service,
            ingestion_service,
            crawler_service,
        )
    ):
        from nptu_assistant.wiring import build_services

        defaults = build_services(settings)
        health_service = health_service or defaults["health_service"]
        chat_service = chat_service or defaults["chat_service"]
        announcement_service = announcement_service or defaults["announcement_service"]
        ingestion_service = ingestion_service or defaults["ingestion_service"]
        crawler_service = crawler_service or defaults["crawler_service"]
    app = FastAPI(title="NPTU 校務資訊助理 API", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Admin-Key", "X-Request-ID"],
    )
    limiter = rate_limiter or InMemoryRateLimiter()
    health_service = health_service or _DefaultHealth(settings)
    chat_service = chat_service or _UnavailableChat()
    announcement_service = announcement_service or _EmptyAnnouncements()
    ingestion_service = ingestion_service or _UnavailableOperation()
    crawler_service = crawler_service or _UnavailableOperation()

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        incoming = request.headers.get("X-Request-ID", "")
        request.state.request_id = incoming if _SAFE_REQUEST_ID.fullmatch(incoming) else str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        logger.info(
            "request_complete",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
            },
        )
        return response

    def error_payload(request: Request, code: str, message: str, details: object = None) -> dict[str, object]:
        return {
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "request_id": getattr(request.state, "request_id", "unknown"),
            }
        }

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(request, exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=error_payload(request, "validation_error", "輸入資料驗證失敗。", exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        message = "找不到指定資源" if exc.status_code == 404 else "HTTP 請求失敗"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(request, code, message),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "request_failed",
            extra={
                "request_id": getattr(request.state, "request_id", "unknown"),
                "method": request.method,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content=error_payload(request, "internal_error", "伺服器發生未預期錯誤"),
        )

    def rate_limit(bucket: str, limit: int) -> Any:
        def dependency(request: Request) -> None:
            key = request.client.host if request.client else "unknown"
            if not limiter.allow(bucket, key, limit=limit, window_seconds=60):
                raise AppError("rate_limit_exceeded", "請求過於頻繁，請稍後再試。", status_code=429)

        return dependency

    def require_admin(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> None:
        if not settings.is_admin_enabled:
            raise AppError("admin_disabled", "管理端點未啟用。", status_code=404)
        if not secrets_match(x_admin_key, settings.admin_api_key.get_secret_value()):
            raise AppError("admin_unauthorized", "管理金鑰無效。", status_code=401)

    @app.get("/health", responses=_error_responses(500, 503))
    def health() -> JSONResponse:
        result = health_service.check()
        return JSONResponse(status_code=503 if result.get("status") == "unhealthy" else 200, content=result)

    @app.post(
        "/v1/chat",
        response_model=ChatResponse,
        responses=_error_responses(422, 429, 500, 503),
        dependencies=[Depends(rate_limit("chat", 20))],
    )
    def chat(payload: ChatRequest) -> ChatResponse:
        return chat_service.answer(payload.question)

    @app.get(
        "/v1/announcements",
        response_model=AnnouncementListResponse,
        responses=_error_responses(422, 429, 500),
        dependencies=[Depends(rate_limit("announcements", 60))],
    )
    def announcements(
        q: str | None = Query(default=None, max_length=200),
        unit: str | None = Query(default=None, max_length=200),
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> AnnouncementListResponse:
        return announcement_service.list(
            q=q,
            unit=unit,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )

    @app.post(
        "/v1/admin/ingest/documents",
        response_model=IngestionSummary,
        responses=_error_responses(401, 404, 429, 500, 503),
        dependencies=[Depends(rate_limit("admin", 5)), Depends(require_admin)],
    )
    def ingest_documents() -> IngestionSummary:
        return ingestion_service.run()

    @app.post(
        "/v1/admin/crawl/announcements",
        response_model=CrawlSummary,
        responses=_error_responses(401, 404, 422, 429, 500, 503),
        dependencies=[Depends(rate_limit("admin", 5)), Depends(require_admin)],
    )
    def crawl_announcements(payload: CrawlRequest | None = None) -> CrawlSummary:
        return crawler_service.run(payload.source_names if payload else None)

    return app


app = create_app()
