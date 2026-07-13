from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nptu_assistant.core.security import is_allowed_nptu_url


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


class ErrorBody(BaseModel):
    code: str
    message: str
    details: object | None = None
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
