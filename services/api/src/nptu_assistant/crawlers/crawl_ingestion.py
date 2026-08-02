from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import Lock
from typing import Final

from pydantic import HttpUrl

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage
from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.service import DocumentRepository
from nptu_assistant.providers.protocols import EmbeddingProvider


class CrawlIngestionStatus(StrEnum):
    CREATED = "created"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CrawlIngestionResult:
    canonical_url: str
    status: CrawlIngestionStatus
    error: str | None = None


class CrawlIngestionService:
    """將已由 crawler 解析的頁面接到既有 document ingestion pipeline。

    這個 adapter 不負責 HTTP、爬蟲狀態或資料庫版本切換；它只負責把
    ``NptuSitePage`` 轉成既有的 metadata/chunk/embedding/save 介面。資料庫
    repository 的 transaction 是最後成功 Document 的保護邊界。
    """

    _DEFAULT_DOCUMENT_TYPE: Final[str] = "official_web_page"

    def __init__(
        self,
        repository: DocumentRepository,
        embedding_provider: EmbeddingProvider,
        *,
        default_unit: str | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._default_unit = default_unit
        # Protect the has_hash -> embed -> save sequence for this adapter. This
        # closes the duplicate-ingestion race when a worker delivers one page
        # more than once through the same service instance.
        self._ingestion_lock = Lock()

    def ingest_page(
        self,
        page: NptuSitePage,
        *,
        unit: str | None = None,
        document_type: str = _DEFAULT_DOCUMENT_TYPE,
    ) -> CrawlIngestionResult:
        """Ingest one crawled page without allowing one page to abort a batch."""

        try:
            raw_text = page.body.strip()
            if not raw_text:
                return CrawlIngestionResult(
                    page.canonical_url,
                    CrawlIngestionStatus.SKIPPED,
                )

            digest = content_hash(raw_text)
            with self._ingestion_lock:
                if self._repository.has_hash(page.canonical_url, digest):
                    return CrawlIngestionResult(
                        page.canonical_url,
                        CrawlIngestionStatus.SKIPPED,
                    )

                chunks = chunk_text(raw_text)
                embeddings = self._embedding_provider.embed(
                    [chunk.content for chunk in chunks]
                )
                if len(chunks) != len(embeddings):
                    raise ValueError("頁面分塊與 embedding 數量不一致")

                resolved_unit = unit or self._default_unit
                if not resolved_unit:
                    raise ValueError("背景頁面 ingestion 缺少 unit")
                metadata = DocumentMetadata(
                    title=page.title,
                    source_url=HttpUrl(page.canonical_url),
                    unit=resolved_unit,
                    published_at=page.published_at,
                    effective_from=page.published_at
                    or datetime.now(timezone.utc).date(),
                    document_type=document_type,
                    version=digest[:12],
                )
                self._repository.save(metadata, raw_text, chunks, embeddings)

            return CrawlIngestionResult(
                page.canonical_url,
                CrawlIngestionStatus.CREATED,
            )
        except Exception as exc:  # noqa: BLE001 - ingestion is a fail-open worker boundary
            return CrawlIngestionResult(
                page.canonical_url,
                CrawlIngestionStatus.FAILED,
                f"{type(exc).__name__}: {exc}",
            )

    def ingest_pages(
        self,
        pages: Iterable[NptuSitePage],
        *,
        unit: str | None = None,
        document_type: str = _DEFAULT_DOCUMENT_TYPE,
    ) -> IngestionSummary:
        """Process pages independently so one fetch/embedding/save failure is local."""

        summary = IngestionSummary()
        for page in pages:
            result = self.ingest_page(
                page,
                unit=unit,
                document_type=document_type,
            )
            if result.status is CrawlIngestionStatus.CREATED:
                summary.created += 1
            elif result.status is CrawlIngestionStatus.SKIPPED:
                summary.skipped += 1
            else:
                summary.failed += 1
                summary.errors.append(
                    f"{result.canonical_url}: {result.error or '未知錯誤'}"
                )
        return summary
