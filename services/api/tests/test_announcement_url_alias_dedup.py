from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    nptu_content_identity,
)
from nptu_assistant.crawlers.adapters.nptu_html import NptuHtmlListAdapter
from nptu_assistant.crawlers.config import load_source_configs
from nptu_assistant.crawlers.refresh import AnnouncementRefreshCoordinator


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"
ENCODED_URL = "https://staf-life.nptu.edu.tw/p/406-1074-200001%2Cr3893.php?Lang=zh-tw"
LITERAL_URL = "https://staf-life.nptu.edu.tw/p/406-1074-200001,r3893.php?Lang=zh-tw"
PAGE_406_URL = "https://staf-life.nptu.edu.tw/p/406-1074-198126,r3893.php?Lang=zh-tw"
PAGE_404_URL = "https://staf-life.nptu.edu.tw/p/404-1074-198126.php?Lang=zh-tw"
OTHER_URL = "https://www.nptu.edu.tw/p/406-1000-200002.php?Lang=zh-tw"
NOW = datetime(2026, 8, 5, 9, 30, tzinfo=timezone.utc)


class _UnexpectedCrawler:
    def run_with_urls(self, source_names: list[str] | None = None) -> object:
        raise AssertionError(f"fresh snapshot must not crawl: {source_names}")


class _FreshAliasRepository:
    def latest_crawled_at(self, source_name: str) -> datetime:
        assert source_name == "nptu-overview"
        return NOW - timedelta(minutes=5)

    def canonical_urls_for_source(self, source_name: str) -> tuple[str, ...]:
        assert source_name == "nptu-overview"
        return (
            ENCODED_URL,
            LITERAL_URL,
            PAGE_406_URL,
            PAGE_404_URL,
            OTHER_URL,
        )

    def record_source_refresh(self, **values: object) -> None:
        raise AssertionError(f"fresh snapshot must not be rewritten: {values}")


def _information_college_adapter() -> NptuHtmlListAdapter:
    config = next(
        item
        for item in load_source_configs(CONFIG_PATH)
        if item.name == "information-college-html"
    )
    return NptuHtmlListAdapter(config)


def test_canonicalizer_treats_encoded_comma_as_nptu_path_alias() -> None:
    assert canonicalize_nptu_url(ENCODED_URL) == LITERAL_URL
    assert canonicalize_nptu_url(LITERAL_URL) == LITERAL_URL


def test_content_identity_collapses_404_and_406_routes_without_rewriting_urls() -> (
    None
):
    assert canonicalize_nptu_url(PAGE_406_URL) == PAGE_406_URL
    assert canonicalize_nptu_url(PAGE_404_URL) == PAGE_404_URL
    assert nptu_content_identity(PAGE_406_URL) == nptu_content_identity(PAGE_404_URL)


def test_content_identity_keeps_distinct_hosts_units_and_content_ids() -> None:
    identity = nptu_content_identity(PAGE_406_URL)

    assert identity != nptu_content_identity(PAGE_406_URL.replace("198126", "198127"))
    assert identity != nptu_content_identity(PAGE_406_URL.replace("1074", "1075"))
    assert identity != nptu_content_identity(
        PAGE_406_URL.replace("staf-life.nptu.edu.tw", "www.nptu.edu.tw")
    )


def test_html_listing_collapses_encoded_and_literal_comma_aliases() -> None:
    encoded = ENCODED_URL.replace("staf-life.nptu.edu.tw", "ccs.nptu.edu.tw")
    literal = LITERAL_URL.replace("staf-life.nptu.edu.tw", "ccs.nptu.edu.tw")
    html = f"""
    <section class="mb">
      <div class="row listBS"><i class="mdate">2026-08-05</i>
        <div class="mtitle"><a href="{encoded}">同一公告</a></div></div>
      <div class="row listBS"><i class="mdate">2026-08-05</i>
        <div class="mtitle"><a href="{literal}">同一公告</a></div></div>
    </section>
    """

    items = _information_college_adapter().parse_listing(html)

    assert len(items) == 1
    assert items[0].canonical_url == literal


def test_html_listing_collapses_404_and_406_content_route_aliases() -> None:
    route_406 = PAGE_406_URL.replace("staf-life.nptu.edu.tw", "ccs.nptu.edu.tw")
    route_404 = PAGE_404_URL.replace("staf-life.nptu.edu.tw", "ccs.nptu.edu.tw")
    html = f"""
    <section class="mb">
      <div class="row listBS"><i class="mdate">2026-08-05</i>
        <div class="mtitle"><a href="{route_406}">得力教育基金會獎助學金</a></div></div>
      <div class="row listBS"><i class="mdate">2026-08-05</i>
        <div class="mtitle"><a href="{route_404}">得力教育基金會獎助學金</a></div></div>
    </section>
    """

    items = _information_college_adapter().parse_listing(html)

    assert len(items) == 1
    assert items[0].canonical_url == route_406


def test_fresh_snapshot_deduplicates_legacy_url_aliases_without_waiting_for_ttl() -> (
    None
):
    coordinator = AnnouncementRefreshCoordinator(
        CONFIG_PATH,
        _UnexpectedCrawler(),  # type: ignore[arg-type]
        _FreshAliasRepository(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.attempted is False
    assert result.succeeded is True
    assert result.canonical_urls == (ENCODED_URL, PAGE_406_URL, OTHER_URL)
