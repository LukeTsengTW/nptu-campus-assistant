from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Protocol
from uuid import UUID

from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.models import AnnouncementCandidate


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AnnouncementSourceIdentity:
    """Incremental 公告寫入所使用的既有、穩定來源識別。"""

    name: str
    url: str
    unit: str
    interval_minutes: int = 60

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("公告來源名稱不得為空")
        if not self.url.strip():
            raise ValueError("公告來源 URL 不得為空")
        if not self.unit.strip():
            raise ValueError("公告來源單位不得為空")
        if self.interval_minutes <= 0:
            raise ValueError("公告來源更新間隔必須大於零")


class IncrementalAnnouncementRepository(Protocol):
    """既有公告 repository 的最小相容介面。

    ``merge_source_announcements`` 是目前 repository 已提供的 additive
    寫入路徑；若主代理在 DB repository 加上
    ``upsert_incremental_announcement``，adapter 會優先使用它，以便在
    跨 process 時由資料庫依 completeness 做 fencing。
    """

    def merge_source_announcements(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
    ) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class AnnouncementPersistenceResult:
    """一次 incremental 公告整合的可觀測結果。"""

    discovered_count: int = 0
    persisted_count: int = 0
    skipped_count: int = 0
    undated_count: int = 0
    failed_count: int = 0
    canonical_urls: tuple[str, ...] = ()
    retryable_urls: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def partial(self) -> bool:
        """Whether this result still has retryable persistence failures."""

        return self.failed_count > 0

    @property
    def warning(self) -> str | None:
        if self.undated_count and not self.failed_count:
            return "公告項目缺少官方發布日期，已標記為 terminal incomplete；後續內容變更時可重新評估。"
        if self.undated_count:
            return "公告資料部分完成，另有缺少官方發布日期的項目已標記為 terminal incomplete。"
        if not self.failed_count:
            return None
        if self.persisted_count:
            return "公告資料部分完成，未完成項目可稍後重試。"
        return "公告資料尚未完成，請稍後重試。"

    def merge(
        self, other: "AnnouncementPersistenceResult"
    ) -> "AnnouncementPersistenceResult":
        return AnnouncementPersistenceResult(
            discovered_count=self.discovered_count + other.discovered_count,
            persisted_count=self.persisted_count + other.persisted_count,
            skipped_count=self.skipped_count + other.skipped_count,
            undated_count=self.undated_count + other.undated_count,
            failed_count=self.failed_count + other.failed_count,
            canonical_urls=tuple(
                dict.fromkeys((*self.canonical_urls, *other.canonical_urls))
            ),
            retryable_urls=tuple(
                dict.fromkeys((*self.retryable_urls, *other.retryable_urls))
            ),
            errors=(*self.errors, *other.errors),
        )


class IncrementalAnnouncementAdapter:
    """把 ``NptuSitePage`` 的 listing/detail 接到既有公告模型。

    這個 adapter 不建立新的公告模型，也不碰 crawler page lease 或
    document ingestion state machine。它只負責：

    * 以 canonical URL 在一批頁面內去重；
    * 將 detail 的較完整標題、正文與日期合併到 listing；
    * 沒有日期時不填入今天或其他推測日期；
    * 每個 URL 獨立提交，讓部分成功可以保留並重試。

    DB repository 若支援 ``upsert_incremental_announcement``，該方法可
    接收 completeness tuple，負責跨 process 的「較完整資料不可被較弱
    listing 覆蓋」保護；沒有該方法時則退回既有 additive repository 介面，
    保持現有呼叫端 backward compatible。
    """

    def __init__(
        self,
        repository: IncrementalAnnouncementRepository,
        *,
        category: str = "單位公告",
    ) -> None:
        self._repository = repository
        self._category = category
        # 只記錄本 adapter instance 已成功寫入的最佳觀測。失敗項目不會
        # 進入這個 cache，因此下一次呼叫仍會真的重試。
        self._best_by_url: dict[str, AnnouncementCandidate] = {}
        self._pending_listings: dict[str, NptuListingItem] = {}

    def persist_page(
        self,
        page: NptuSitePage,
        source: AnnouncementSourceIdentity,
        *,
        crawled_at: datetime | None = None,
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
        page_content_hash: str | None = None,
    ) -> AnnouncementPersistenceResult:
        return self.persist_pages(
            (page,),
            source,
            crawled_at=crawled_at,
            source_page_url=page.canonical_url,
            page_id=page_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            page_content_hash=page_content_hash,
        )

    def persist_pages(
        self,
        pages: Iterable[NptuSitePage],
        source: AnnouncementSourceIdentity,
        *,
        crawled_at: datetime | None = None,
        source_page_url: str | None = None,
        page_id: UUID | str | None = None,
        lease_owner: str | None = None,
        lease_token: UUID | str | None = None,
        lease_expires_at: datetime | None = None,
        page_content_hash: str | None = None,
    ) -> AnnouncementPersistenceResult:
        listing_items: dict[str, NptuListingItem] = {}
        detail_pages: dict[str, NptuSitePage] = {}
        ordered_urls: list[str] = []

        for page in pages:
            if page.role is UnitAnnouncementPageRole.LISTING or page.announcement_items:
                for item in page.announcement_items:
                    if item.canonical_url not in ordered_urls:
                        ordered_urls.append(item.canonical_url)
                    listing_items[item.canonical_url] = self._merge_listing_item(
                        listing_items.get(item.canonical_url), item
                    )
                continue
            if page.role is UnitAnnouncementPageRole.DETAIL:
                if page.canonical_url not in ordered_urls:
                    ordered_urls.append(page.canonical_url)
                detail_pages[page.canonical_url] = self._merge_detail_page(
                    detail_pages.get(page.canonical_url), page
                )

        result = AnnouncementPersistenceResult(discovered_count=len(ordered_urls))
        persisted_at = crawled_at or datetime.now(timezone.utc)
        for canonical_url in ordered_urls:
            item = listing_items.get(canonical_url) or self._pending_listings.get(
                canonical_url
            )
            detail = detail_pages.get(canonical_url)
            candidate = self._candidate_for(
                item,
                detail,
                source=source,
            )
            if candidate is None:
                if item is not None:
                    self._pending_listings[canonical_url] = item
                result = result.merge(AnnouncementPersistenceResult(undated_count=1))
                continue

            previous = self._best_by_url.get(canonical_url)
            is_detail = detail is not None
            merged = self._merge_candidate(previous, candidate, is_detail=is_detail)
            if previous is not None and merged == previous:
                result = result.merge(
                    AnnouncementPersistenceResult(
                        skipped_count=1,
                        canonical_urls=(canonical_url,),
                    )
                )
                continue

            try:
                self._persist_candidate(
                    merged,
                    source=source,
                    crawled_at=persisted_at,
                    page_id=page_id,
                    source_page_url=source_page_url,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_expires_at=lease_expires_at,
                    page_content_hash=page_content_hash,
                )
            except Exception as exc:  # noqa: BLE001 - one URL must not abort a batch
                message = f"{canonical_url}: {type(exc).__name__}: {exc}"
                logger.exception(
                    "incremental announcement persistence failed",
                    extra={
                        "announcement_source_name": source.name,
                        "announcement_url": canonical_url,
                        "announcement_retryable": True,
                    },
                )
                result = result.merge(
                    AnnouncementPersistenceResult(
                        failed_count=1,
                        retryable_urls=(canonical_url,),
                        errors=(message,),
                    )
                )
                continue

            self._best_by_url[canonical_url] = merged
            self._pending_listings.pop(canonical_url, None)
            result = result.merge(
                AnnouncementPersistenceResult(
                    persisted_count=1,
                    canonical_urls=(canonical_url,),
                )
            )

        logger.info(
            "incremental announcement persistence complete",
            extra={
                "announcement_source_name": source.name,
                "announcement_discovered_count": result.discovered_count,
                "announcement_persisted_count": result.persisted_count,
                "announcement_skipped_count": result.skipped_count,
                "announcement_undated_count": result.undated_count,
                "announcement_failed_count": result.failed_count,
                "announcement_retryable_count": len(result.retryable_urls),
            },
        )
        if not result.partial and result.discovered_count:
            mark_success = getattr(
                self._repository, "mark_incremental_source_success", None
            )
            if callable(mark_success):
                mark_success(source_name=source.name, crawled_at=persisted_at)
        return result

    def _persist_candidate(
        self,
        candidate: AnnouncementCandidate,
        *,
        source: AnnouncementSourceIdentity,
        crawled_at: datetime,
        page_id: UUID | str | None,
        source_page_url: str | None,
        lease_owner: str | None,
        lease_token: UUID | str | None,
        lease_expires_at: datetime | None,
        page_content_hash: str | None,
    ) -> None:
        enriched_upsert = getattr(
            self._repository, "upsert_incremental_announcement", None
        )
        if callable(enriched_upsert):
            enriched_upsert(
                candidate,
                source_name=source.name,
                source_url=source.url,
                source_unit=source.unit,
                interval_minutes=source.interval_minutes,
                crawled_at=crawled_at,
                completeness=self._completeness(candidate),
                page_id=page_id,
                source_page_url=source_page_url,
                lease_owner=lease_owner,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                page_content_hash=page_content_hash,
            )
            return

        statuses = self._repository.merge_source_announcements(
            [candidate],
            source_name=source.name,
            source_url=source.url,
            source_unit=source.unit,
            interval_minutes=source.interval_minutes,
            crawled_at=crawled_at,
        )
        if len(statuses) != 1:
            raise RuntimeError("公告 repository 回傳的批次結果數量不一致")

    def _candidate_for(
        self,
        listing: NptuListingItem | None,
        detail: NptuSitePage | None,
        *,
        source: AnnouncementSourceIdentity,
    ) -> AnnouncementCandidate | None:
        if listing is None and detail is None:
            return None
        published_at = detail.published_at if detail is not None else None
        if published_at is None and listing is not None:
            published_at = listing.published_at
        if published_at is None:
            return None

        listing_title = listing.title.strip() if listing is not None else ""
        detail_title = self._usable_detail_title(detail)
        title = detail_title or listing_title
        if not title:
            return None

        listing_body = (
            listing.summary.strip() if listing is not None and listing.summary else ""
        ) or listing_title
        detail_body = detail.body.strip() if detail is not None else ""
        body = detail_body if len(detail_body) >= len(listing_body) else listing_body
        if not body:
            return None

        if detail is not None:
            canonical_url = detail.canonical_url
        elif listing is not None:
            canonical_url = listing.canonical_url
        else:
            return None
        return AnnouncementCandidate(
            title=title,
            canonical_url=canonical_url,
            unit=source.unit,
            category=self._category,
            published_at=published_at,
            deadline_at=None,
            body=body,
        )

    @staticmethod
    def _usable_detail_title(page: NptuSitePage | None) -> str:
        if page is None:
            return ""
        title = page.title.strip()
        return title if title and title != page.canonical_url else ""

    @staticmethod
    def _merge_candidate(
        previous: AnnouncementCandidate | None,
        incoming: AnnouncementCandidate,
        *,
        is_detail: bool,
    ) -> AnnouncementCandidate:
        if previous is None:
            return incoming
        title = previous.title
        if is_detail and incoming.title.strip():
            title = incoming.title
        elif len(incoming.title.strip()) > len(previous.title.strip()):
            title = incoming.title
        body = (
            incoming.body
            if len(incoming.body.strip()) > len(previous.body.strip())
            else previous.body
        )
        published_at = incoming.published_at if is_detail else previous.published_at
        return AnnouncementCandidate(
            title=title,
            canonical_url=previous.canonical_url,
            unit=previous.unit or incoming.unit,
            category=previous.category or incoming.category,
            published_at=published_at,
            deadline_at=previous.deadline_at or incoming.deadline_at,
            body=body,
            warning=previous.warning or incoming.warning,
        )

    @staticmethod
    def _merge_listing_item(
        previous: NptuListingItem | None,
        incoming: NptuListingItem,
    ) -> NptuListingItem:
        if previous is None:
            return incoming
        return NptuListingItem(
            title=(
                incoming.title
                if len(incoming.title) > len(previous.title)
                else previous.title
            ),
            canonical_url=previous.canonical_url,
            published_at=previous.published_at or incoming.published_at,
            summary=(
                incoming.summary
                if len(incoming.summary) > len(previous.summary)
                else previous.summary
            ),
            anchor_text=(
                incoming.anchor_text
                if len(incoming.anchor_text) > len(previous.anchor_text)
                else previous.anchor_text
            ),
            order=min(previous.order, incoming.order),
        )

    @staticmethod
    def _merge_detail_page(
        previous: NptuSitePage | None,
        incoming: NptuSitePage,
    ) -> NptuSitePage:
        if previous is None:
            return incoming
        return incoming if len(incoming.body) > len(previous.body) else previous

    @staticmethod
    def _completeness(candidate: AnnouncementCandidate) -> tuple[int, int, int, int]:
        return (
            len(candidate.body.strip()),
            len(candidate.title.strip()),
            int(candidate.published_at is not None),
            int(candidate.deadline_at is not None),
        )
