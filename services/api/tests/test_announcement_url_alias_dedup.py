from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nptu_assistant.core.security import canonicalize_nptu_url
from nptu_assistant.crawlers.adapters.nptu_html import NptuHtmlListAdapter
from nptu_assistant.crawlers.config import load_source_configs
from nptu_assistant.crawlers.refresh import AnnouncementRefreshCoordinator


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"
ENCODED_URL = "https://staf-life.nptu.edu.tw/p/406-1074-200001%2Cr3893.php?Lang=zh-tw"
LITERAL_URL = "https://staf-life.nptu.edu.tw/p/406-1074-200001,r3893.php?Lang=zh-tw"
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
        return (ENCODED_URL, LITERAL_URL, OTHER_URL)

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
    assert result.canonical_urls == (ENCODED_URL, OTHER_URL)
