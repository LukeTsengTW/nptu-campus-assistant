from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import Lock
from typing import Final
from uuid import UUID

from pydantic import HttpUrl

from nptu_assistant.api.schemas import IngestionSummary
from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.announcement_adapter import (
    AnnouncementSourceIdentity,
    IncrementalAnnouncementAdapter,
)
from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.cleaning import content_hash
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.service import DocumentRepository
from nptu_assistant.providers.protocols import EmbeddingProvider


class CrawlIngestionStatus(StrEnum):
    CREATED = "created"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class CrawlIngestionState(StrEnum):
    """Persisted page-ingestion state, separate from fetched crawl state."""

    PENDING = "pending"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    SUCCESS = "success"


@dataclass(frozen=True, slots=True)
class CrawlIngestionResult:
    canonical_url: str
    status: CrawlIngestionStatus
    error: str | None = None
    content_hash: str | None = None
    announcement_persisted: int = 0
    announcement_failed: int = 0
    announcement_incomplete: int = 0


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
        announcement_adapter: IncrementalAnnouncementAdapter | None = None,
        announcement_source_resolver: Callable[
            [NptuSitePage, str | None], AnnouncementSourceIdentity | None
        ]
        | None = None,
    ) -> None:
        self._repository = repository
        self._embedding_provider = embedding_provider
        self._default_unit = default_unit
        self._announcement_adapter = announcement_adapter
        self._announcement_source_resolver = announcement_source_resolver
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
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
        allow_unleased: bool = False,
    ) -> CrawlIngestionResult:
        """Ingest one crawled page without allowing one page to abort a batch."""

        raw_text = ""
        try:
            if not self._has_lease_context(page_id, lease_owner, lease_token):
                if not allow_unleased:
                    raise RuntimeError(
                        "背景 crawler ingestion 必須有 page lease；"
                        "live fallback 需明確設定 allow_unleased"
                    )
            raw_text = page.body.strip()
            if not raw_text:
                return CrawlIngestionResult(
                    page.canonical_url,
                    CrawlIngestionStatus.SKIPPED,
                )

            digest = content_hash(raw_text)
            with self._ingestion_lock:
                state = self._begin_ingestion(
                    page.canonical_url,
                    digest,
                    page_id=page_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                )
                if state == CrawlIngestionState.SUCCESS.value:
                    return CrawlIngestionResult(
                        page.canonical_url,
                        CrawlIngestionStatus.SKIPPED,
                        content_hash=digest,
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
                save_idempotent = getattr(self._repository, "save_idempotent", None)
                if callable(save_idempotent):
                    try:
                        created = bool(
                            save_idempotent(
                                metadata,
                                raw_text,
                                chunks,
                                embeddings,
                                **self._lease_context(
                                    page_id=page_id,
                                    lease_owner=lease_owner,
                                    lease_token=lease_token,
                                    lease_expires_at=lease_expires_at,
                                ),
                            )
                        )
                    except TypeError as exc:
                        if "unexpected keyword" not in str(exc).casefold():
                            raise
                        created = bool(
                            save_idempotent(metadata, raw_text, chunks, embeddings)
                        )
                else:
                    # Legacy repositories retain the original adapter contract.
                    if self._repository.has_hash(page.canonical_url, digest):
                        created = False
                    else:
                        self._repository.save(metadata, raw_text, chunks, embeddings)
                        created = True
                self._complete_ingestion(
                    page.canonical_url,
                    digest,
                    page_id=page_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                )
                announcement_persisted = 0
                announcement_failed = 0
                announcement_incomplete = 0
                announcement_partial = False
                if self._announcement_adapter is not None and page.role in {
                    UnitAnnouncementPageRole.LISTING,
                    UnitAnnouncementPageRole.DETAIL,
                }:
                    source = (
                        self._announcement_source_resolver(page, resolved_unit)
                        if self._announcement_source_resolver is not None
                        else None
                    )
                    if source is None:
                        raise RuntimeError(
                            "公告頁缺少資料庫 allowlist source identity，無法安全持久化"
                        )
                    try:
                        announcement_result = self._announcement_adapter.persist_page(
                            page,
                            source,
                            crawled_at=datetime.now(timezone.utc),
                            page_id=page_id,
                            lease_owner=lease_owner,
                            lease_token=lease_token,
                            lease_expires_at=lease_expires_at,
                            page_content_hash=digest,
                        )
                    except Exception as exc:  # noqa: BLE001 - retain retry state
                        announcement_persisted = 0
                        announcement_failed = 1
                        announcement_partial = True
                        self._fail_announcement_ingestion(
                            page.canonical_url,
                            digest,
                            page_id=page_id,
                            lease_owner=lease_owner,
                            lease_token=lease_token,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    else:
                        announcement_persisted = announcement_result.persisted_count
                        announcement_failed = announcement_result.failed_count
                        announcement_incomplete = announcement_result.undated_count
                        announcement_partial = announcement_result.failed_count > 0
                        if announcement_result.failed_count:
                            self._fail_announcement_ingestion(
                                page.canonical_url,
                                digest,
                                page_id=page_id,
                                lease_owner=lease_owner,
                                lease_token=lease_token,
                                error=announcement_result.warning
                                or "公告資料部分持久化失敗",
                            )
                        elif announcement_result.undated_count:
                            self._complete_announcement_ingestion(
                                page.canonical_url,
                                digest,
                                page_id=page_id,
                                lease_owner=lease_owner,
                                lease_token=lease_token,
                                status=CrawlIngestionState.INCOMPLETE.value,
                                warning=announcement_result.warning,
                            )
                        else:
                            self._complete_announcement_ingestion(
                                page.canonical_url,
                                digest,
                                page_id=page_id,
                                lease_owner=lease_owner,
                                lease_token=lease_token,
                            )

            return CrawlIngestionResult(
                page.canonical_url,
                CrawlIngestionStatus.PARTIAL
                if announcement_partial
                else CrawlIngestionStatus.INCOMPLETE
                if announcement_incomplete
                else CrawlIngestionStatus.CREATED
                if created
                else CrawlIngestionStatus.SKIPPED,
                error=("公告持久化部分成功，將重試" if announcement_partial else None),
                content_hash=digest,
                announcement_persisted=announcement_persisted,
                announcement_failed=announcement_failed,
                announcement_incomplete=announcement_incomplete,
            )
        except Exception as exc:  # noqa: BLE001 - ingestion is a fail-open worker boundary
            try:
                self._fail_ingestion(
                    page.canonical_url,
                    content_hash(raw_text) if raw_text else None,
                    page_id=page_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception as state_exc:  # noqa: BLE001 - preserve original failure
                if "lease" in str(state_exc).casefold():
                    exc = RuntimeError(f"{exc}; ingestion lease fencing: {state_exc}")
            return CrawlIngestionResult(
                page.canonical_url,
                CrawlIngestionStatus.FAILED,
                f"{type(exc).__name__}: {exc}",
                content_hash=content_hash(page.body.strip())
                if page.body.strip()
                else None,
            )

    def needs_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> bool:
        """Return whether this fetched hash still needs a terminal ingestion.

        The optional repository method is DB-backed when available.  The
        legacy fallback keeps the existing in-memory adapter contract.
        """

        checker = getattr(self._repository, "needs_ingestion", None)
        if callable(checker):
            return bool(
                checker(
                    canonical_url,
                    digest,
                    page_id=page_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                )
            )
        return not self._repository.has_hash(canonical_url, digest)

    def _begin_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
        lease_expires_at: datetime | None,
    ) -> str:
        begin = getattr(self._repository, "begin_ingestion", None)
        if not callable(begin) or not self._has_lease_context(
            page_id, lease_owner, lease_token
        ):
            if self._repository.has_hash(canonical_url, digest):
                return CrawlIngestionState.SUCCESS.value
            return CrawlIngestionState.PENDING.value
        return str(
            begin(
                canonical_url,
                digest,
                page_id=page_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
            )
        )

    def _complete_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
        lease_expires_at: datetime | None,
    ) -> None:
        complete = getattr(self._repository, "complete_ingestion", None)
        if not callable(complete) or not self._has_lease_context(
            page_id, lease_owner, lease_token
        ):
            return
        applied = complete(
            canonical_url,
            digest,
            page_id=page_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
        )
        if applied is False:
            raise RuntimeError("ingestion lease 已失效，拒絕完成 ingestion")

    def _complete_announcement_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
        status: str = CrawlIngestionState.SUCCESS.value,
        warning: str | None = None,
    ) -> None:
        complete = getattr(self._repository, "complete_announcement_ingestion", None)
        if not callable(complete) or not self._has_lease_context(
            page_id, lease_owner, lease_token
        ):
            return
        try:
            applied = complete(
                canonical_url,
                digest,
                page_id=page_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                status=status,
                warning=warning,
            )
        except TypeError as exc:
            # Existing repository doubles may still implement the old success
            # signature.  Never silently downgrade the new incomplete state.
            if "unexpected keyword" not in str(exc).casefold():
                raise
            try:
                applied = complete(
                    canonical_url,
                    digest,
                    page_id=page_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    status=status,
                )
            except TypeError:
                if status != CrawlIngestionState.SUCCESS.value:
                    raise
                applied = complete(
                    canonical_url,
                    digest,
                    page_id=page_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                )
        if applied is False:
            raise RuntimeError("公告 ingestion lease 已失效，拒絕完成公告持久化")

    def _fail_announcement_ingestion(
        self,
        canonical_url: str,
        digest: str,
        *,
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: str | UUID | None,
        error: str,
    ) -> None:
        fail = getattr(self._repository, "fail_announcement_ingestion", None)
        if not callable(fail) or not self._has_lease_context(
            page_id, lease_owner, lease_token
        ):
            return
        applied = fail(
            canonical_url,
            digest,
            page_id=page_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            error=error,
        )
        if applied is False:
            raise RuntimeError("公告 ingestion lease 已失效，拒絕記錄公告失敗")

    def _fail_ingestion(
        self,
        canonical_url: str,
        digest: str | None,
        *,
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
        lease_expires_at: datetime | None,
        error: str,
    ) -> None:
        fail = getattr(self._repository, "fail_ingestion", None)
        if (
            not callable(fail)
            or digest is None
            or not self._has_lease_context(page_id, lease_owner, lease_token)
        ):
            return
        applied = fail(
            canonical_url,
            digest,
            page_id=page_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            error=error,
        )
        if applied is False:
            raise RuntimeError("ingestion lease 已失效，拒絕寫入失敗狀態")

    @staticmethod
    def _has_lease_context(
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
    ) -> bool:
        return page_id is not None and bool(lease_owner) and lease_token is not None

    @classmethod
    def _lease_context(
        cls,
        *,
        page_id: UUID | str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
        lease_expires_at: datetime | None,
    ) -> dict[str, object]:
        if not cls._has_lease_context(page_id, lease_owner, lease_token):
            return {}
        return {
            "page_id": page_id,
            "lease_owner": lease_owner,
            "lease_token": lease_token,
            "lease_expires_at": lease_expires_at,
        }

    def ingest_pages(
        self,
        pages: Iterable[NptuSitePage],
        *,
        unit: str | None = None,
        document_type: str = _DEFAULT_DOCUMENT_TYPE,
        allow_unleased: bool = False,
    ) -> IngestionSummary:
        """Process pages independently so one fetch/embedding/save failure is local."""

        summary = IngestionSummary()
        for page in pages:
            result = self.ingest_page(
                page,
                unit=unit,
                document_type=document_type,
                allow_unleased=allow_unleased,
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
