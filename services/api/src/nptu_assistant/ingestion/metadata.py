from __future__ import annotations

from datetime import date

from pydantic import BaseModel, HttpUrl, model_validator

from nptu_assistant.core.security import is_allowed_nptu_url


class DocumentMetadata(BaseModel):
    title: str
    source_url: HttpUrl
    unit: str
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    document_type: str
    version: str

    @model_validator(mode="after")
    def validate_official_metadata(self) -> "DocumentMetadata":
        if not is_allowed_nptu_url(str(self.source_url)):
            raise ValueError("source_url 必須是 NPTU 官方 HTTPS 網址")
        if self.published_at is None and self.effective_from is None:
            raise ValueError("必須提供 published_at 或 effective_from")
        return self
