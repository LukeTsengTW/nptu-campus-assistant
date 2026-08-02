from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nptu_assistant.db.base import Base

if TYPE_CHECKING:
    from nptu_assistant.db.models import SitePage


class SiteCrawlAttempt(Base):
    """一個 site page 的可追蹤 crawl 嘗試與其 HTTP 結果。"""

    __tablename__ = "site_crawl_attempts"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('running', 'success_changed', 'success_unchanged', "
            "'not_modified', 'failed_transient', 'failed_permanent', 'blocked', "
            "'excluded', 'lease_lost')",
            name="ck_site_crawl_attempts_outcome",
        ),
        Index(
            "ix_site_crawl_attempts_page_started_at",
            "site_page_id",
            "started_at",
        ),
        Index("ix_site_crawl_attempts_outcome", "outcome"),
        Index("ix_site_crawl_attempts_lease_token", "lease_token"),
        Index("ix_site_crawl_attempts_worker_started_at", "worker_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    site_page_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("site_pages.id", ondelete="CASCADE"), nullable=False
    )
    lease_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="running"
    )
    http_status: Mapped[int | None] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(255))
    content_length: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    content_changed: Mapped[bool | None] = mapped_column(Boolean)
    links_discovered: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    ingestion_performed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(500))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_kind: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))

    site_page: Mapped[SitePage] = relationship(
        "SitePage", back_populates="crawl_attempts"
    )
