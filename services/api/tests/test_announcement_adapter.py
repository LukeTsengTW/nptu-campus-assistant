from __future__ import annotations

from datetime import date, datetime, timezone

from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.announcement_adapter import (
    AnnouncementSourceIdentity,
    IncrementalAnnouncementAdapter,
)
from nptu_assistant.crawlers.models import AnnouncementCandidate


URL = "https://ccs.nptu.edu.tw/p/406-1025-197412.php?Lang=zh-tw"
LISTING_URL = "https://ccs.nptu.edu.tw/p/403-1025-1019.php?Lang=zh-tw"
SOURCE = AnnouncementSourceIdentity(
    name="information-college-html",
    url=LISTING_URL,
    unit="資訊學院",
)
CRAWLED_AT = datetime(2026, 8, 3, tzinfo=timezone.utc)


class MemoryAnnouncementRepository:
    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        self.items: dict[str, AnnouncementCandidate] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_urls = set(fail_urls or ())
        self.successful_source_refreshes: list[tuple[str, datetime]] = []

    def merge_source_announcements(
        self,
        candidates: list[AnnouncementCandidate],
        *,
        source_name: str,
        source_url: str,
        source_unit: str,
        interval_minutes: int,
        crawled_at: datetime,
    ) -> list[str]:
        del source_url, source_unit, interval_minutes, crawled_at
        self.calls.append(
            (source_name, tuple(item.canonical_url for item in candidates))
        )
        candidate = candidates[0]
        if candidate.canonical_url in self.fail_urls:
            raise RuntimeError("暫時性資料庫錯誤")
        status = "updated" if candidate.canonical_url in self.items else "created"
        self.items[candidate.canonical_url] = candidate
        return [status]

    def mark_incremental_source_success(
        self,
        *,
        source_name: str,
        crawled_at: datetime,
    ) -> None:
        self.successful_source_refreshes.append((source_name, crawled_at))


def listing_item(
    *,
    published_at: date | None = date(2026, 7, 18),
    summary: str = "列表摘要",
    title: str = "資訊學院公告",
    canonical_url: str = URL,
) -> NptuListingItem:
    return NptuListingItem(
        title=title,
        canonical_url=canonical_url,
        published_at=published_at,
        summary=summary,
        anchor_text=title,
        order=0,
    )


def listing_page(*items: NptuListingItem) -> NptuSitePage:
    return NptuSitePage(
        title="最新公告",
        canonical_url=LISTING_URL,
        body="最新公告列表",
        published_at=None,
        links=tuple(item.canonical_url for item in items),
        role=UnitAnnouncementPageRole.LISTING,
        announcement_items=items,
    )


def detail_page(
    *,
    published_at: date | None = date(2026, 7, 19),
    body: str = "完整公告正文，包含活動說明、申請方式與注意事項。",
    title: str = "資訊學院公告完整標題",
) -> NptuSitePage:
    return NptuSitePage(
        title=title,
        canonical_url=URL,
        body=body,
        published_at=published_at,
        links=(),
        role=UnitAnnouncementPageRole.DETAIL,
    )


def test_listing_is_canonical_deduplicated_and_upserted_once() -> None:
    repository = MemoryAnnouncementRepository()
    adapter = IncrementalAnnouncementAdapter(repository)

    result = adapter.persist_page(
        listing_page(
            listing_item(summary="短摘要", canonical_url=URL),
            listing_item(summary="較完整的列表摘要", canonical_url=URL),
        ),
        SOURCE,
        crawled_at=CRAWLED_AT,
    )

    assert result.discovered_count == 1
    assert result.persisted_count == 1
    assert result.failed_count == 0
    assert result.canonical_urls == (URL,)
    assert len(repository.calls) == 1
    assert repository.items[URL].body == "較完整的列表摘要"


def test_detail_enriches_listing_and_later_listing_does_not_downgrade_it() -> None:
    repository = MemoryAnnouncementRepository()
    adapter = IncrementalAnnouncementAdapter(repository)

    listing_result = adapter.persist_page(
        listing_page(listing_item()), SOURCE, crawled_at=CRAWLED_AT
    )
    detail_result = adapter.persist_page(detail_page(), SOURCE, crawled_at=CRAWLED_AT)
    later_listing_result = adapter.persist_page(
        listing_page(listing_item(summary="重新抓到的短摘要")),
        SOURCE,
        crawled_at=CRAWLED_AT,
    )

    assert listing_result.persisted_count == 1
    assert detail_result.persisted_count == 1
    assert later_listing_result.skipped_count == 1
    assert repository.items[URL].title == "資訊學院公告完整標題"
    assert (
        repository.items[URL].body == "完整公告正文，包含活動說明、申請方式與注意事項。"
    )
    assert repository.items[URL].published_at == date(2026, 7, 19)


def test_listing_and_detail_in_one_batch_use_the_more_complete_detail() -> None:
    repository = MemoryAnnouncementRepository()
    adapter = IncrementalAnnouncementAdapter(repository)

    result = adapter.persist_pages(
        (listing_page(listing_item()), detail_page()),
        SOURCE,
        crawled_at=CRAWLED_AT,
    )

    assert result.discovered_count == 1
    assert result.persisted_count == 1
    assert len(repository.calls) == 1
    assert repository.items[URL].body.startswith("完整公告正文")
    assert repository.items[URL].published_at == date(2026, 7, 19)


def test_missing_date_is_observable_and_never_fabricated() -> None:
    repository = MemoryAnnouncementRepository()
    adapter = IncrementalAnnouncementAdapter(repository)
    undated_listing = listing_page(listing_item(published_at=None))

    first = adapter.persist_page(undated_listing, SOURCE, crawled_at=CRAWLED_AT)
    second = adapter.persist_page(
        detail_page(published_at=None), SOURCE, crawled_at=CRAWLED_AT
    )

    assert first.undated_count == 1
    assert second.undated_count == 1
    assert len(repository.successful_source_refreshes) == 2
    assert "terminal incomplete" in (first.warning or "")
    assert repository.items == {}
    assert date.today() not in {
        candidate.published_at for candidate in repository.items.values()
    }


def test_dated_and_undated_listing_advances_source_without_retrying_undated_item() -> (
    None
):
    undated_url = "https://ccs.nptu.edu.tw/p/406-1025-197413.php?Lang=zh-tw"
    repository = MemoryAnnouncementRepository()
    adapter = IncrementalAnnouncementAdapter(repository)
    page = listing_page(
        listing_item(canonical_url=URL),
        listing_item(canonical_url=undated_url, published_at=None),
    )

    first = adapter.persist_page(page, SOURCE, crawled_at=CRAWLED_AT)
    second = adapter.persist_page(page, SOURCE, crawled_at=CRAWLED_AT)

    assert first.persisted_count == 1
    assert first.undated_count == 1
    assert first.partial is False
    assert second.skipped_count == 1
    assert second.undated_count == 1
    assert second.failed_count == 0
    assert len(repository.successful_source_refreshes) == 2
    assert undated_url not in repository.items


def test_undated_listing_can_be_completed_when_detail_later_supplies_date() -> None:
    repository = MemoryAnnouncementRepository()
    adapter = IncrementalAnnouncementAdapter(repository)

    adapter.persist_page(
        listing_page(listing_item(published_at=None)), SOURCE, crawled_at=CRAWLED_AT
    )
    result = adapter.persist_page(
        detail_page(published_at=date(2026, 7, 20)),
        SOURCE,
        crawled_at=CRAWLED_AT,
    )

    assert result.persisted_count == 1
    assert result.undated_count == 0
    assert repository.items[URL].published_at == date(2026, 7, 20)


def test_partial_failure_keeps_failed_url_retryable() -> None:
    second_url = "https://ccs.nptu.edu.tw/p/406-1025-197411.php?Lang=zh-tw"
    repository = MemoryAnnouncementRepository(fail_urls={second_url})
    adapter = IncrementalAnnouncementAdapter(repository)
    page = listing_page(
        listing_item(canonical_url=URL),
        listing_item(canonical_url=second_url, title="第二則公告"),
    )

    partial = adapter.persist_page(page, SOURCE, crawled_at=CRAWLED_AT)
    assert partial.persisted_count == 1
    assert partial.failed_count == 1
    assert partial.retryable_urls == (second_url,)
    assert partial.partial is True
    assert URL in repository.items
    assert second_url not in repository.items

    repository.fail_urls.clear()
    retry = adapter.persist_page(page, SOURCE, crawled_at=CRAWLED_AT)
    assert retry.persisted_count == 1
    assert retry.skipped_count == 1
    assert retry.failed_count == 0
    assert second_url in repository.items
