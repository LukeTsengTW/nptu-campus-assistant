from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_nptu_url


class AnswerType(StrEnum):
    OFFICIAL_DOCUMENT = "official_document"
    ANNOUNCEMENT = "announcement"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("question", mode="before")
    @classmethod
    def strip_question(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class SourceReference(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    kind: AnswerType
    title: str
    url: str
    unit: str
    published_at: date | None
    source_type: Literal["official"] = "official"

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not is_allowed_nptu_url(value):
            raise ValueError("來源 URL 必須是 NPTU 官方 HTTPS 網址")
        return value


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    answer_type: AnswerType
    confidence: Confidence
    sources: list[SourceReference]
    warning: str | None = None


class AnnouncementItem(BaseModel):
    id: str
    title: str
    unit: str
    category: str | None = None
    published_at: date
    deadline_at: date | None = None
    canonical_url: str


class AnnouncementListResponse(BaseModel):
    items: list[AnnouncementItem]
    page: int
    page_size: int
    total: int


class CrawlRequest(BaseModel):
    source_names: list[str] | None = None

    @field_validator("source_names")
    @classmethod
    def validate_source_names(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) > 20:
            raise ValueError("一次最多指定 20 個來源")
        return value


class IngestionSummary(BaseModel):
    created: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)


class CrawlSummary(BaseModel):
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)


class CrawlScheduleRequest(BaseModel):
    source_names: list[str] | None = None
    urls: list[str] | None = Field(default=None, max_length=100)
    unit: str | None = Field(default=None, max_length=200)
    host: str | None = Field(default=None, max_length=255)
    page_type: str | None = Field(default=None, max_length=50)
    run_at: datetime | None = None
    delay_seconds: float = Field(default=0, ge=0, le=86400)
    dry_run: bool = False

    @field_validator("source_names")
    @classmethod
    def validate_source_names(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) > 20:
            raise ValueError("一次最多指定 20 個來源")
        return value

    @field_validator("urls")
    @classmethod
    def validate_urls(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized: list[str] = []
        seen: set[str] = set()
        for url in value:
            if not is_allowed_nptu_url(url):
                raise ValueError("排程 URL 必須是 NPTU 官方 HTTPS 網址")
            canonical = canonicalize_nptu_url(url)
            if canonical not in seen:
                normalized.append(canonical)
                seen.add(canonical)
        return normalized


class CrawlStatusResponse(BaseModel):
    status: str
    enabled: bool = True
    interval_seconds: float | None = None
    run_id: str | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    runs_total: int = 0
    successes_total: int = 0
    failures_total: int = 0
    schedules_total: int = 0
    queue_depth: int = 0
    last_run_status: str | None = None
    last_sources_attempted: int = 0
    last_sources_succeeded: int = 0
    last_sources_failed: int = 0
    last_created: int = 0
    last_updated: int = 0
    last_unchanged: int = 0
    last_failed: int = 0
    last_errors: list[str] = Field(default_factory=list)
    last_duration_ms: float | None = None
    next_run_at: datetime | None = None
    pending_schedule_id: str | None = None
    dry_run: bool = False
    pending: int = 0
    due: int = 0
    leased: int = 0
    failed: int = 0
    blocked: int = 0
    pending_ingestion: int = 0
    active_workers: int = 0
    next_due_at: datetime | None = None
    recent_attempts: dict[str, int] = Field(default_factory=dict)


class CrawlScheduleResponse(BaseModel):
    status: str = "scheduled"
    schedule_id: str
    scheduled_at: datetime
    run_at: datetime
    source_names: list[str] = Field(default_factory=list)
    dry_run: bool = False
    scheduled_pages: int = 0


class SiteMapSyncResponse(BaseModel):
    seen: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    links_created: int = 0


class ErrorBody(BaseModel):
    code: str
    message: str
    details: object | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
