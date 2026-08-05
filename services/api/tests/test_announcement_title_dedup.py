from __future__ import annotations

from datetime import date
from pathlib import Path

from nptu_assistant.crawlers.adapters.nptu_html import NptuHtmlListAdapter
from nptu_assistant.crawlers.announcement_identity import (
    announcement_title_identity,
    normalize_announcement_text,
)
from nptu_assistant.crawlers.config import load_source_configs


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"


def _adapter() -> NptuHtmlListAdapter:
    config = next(
        item
        for item in load_source_configs(CONFIG_PATH)
        if item.name == "information-college-html"
    )
    return NptuHtmlListAdapter(config)


def _listing(*rows: tuple[str, str, str]) -> str:
    items = "".join(
        f"""
        <div class="row listBS">
          <i class="mdate">{published_at}</i>
          <div class="mtitle"><a href="{url}">{title}</a></div>
        </div>
        """
        for published_at, url, title in rows
    )
    return f'<section class="mb">{items}</section>'


def test_normalize_announcement_text_unifies_width_case_and_whitespace() -> None:
    assert normalize_announcement_text(" ＡＢＣ　 Scholarship  ") == "abc scholarship"


def test_title_identity_keeps_different_dates_or_units_distinct() -> None:
    base = announcement_title_identity(
        title="同名公告",
        published_at=date(2026, 8, 5),
        unit="生活輔導組",
    )

    assert base != announcement_title_identity(
        title="同名公告",
        published_at=date(2026, 8, 6),
        unit="生活輔導組",
    )
    assert base != announcement_title_identity(
        title="同名公告",
        published_at=date(2026, 8, 5),
        unit="教務處",
    )


def test_listing_deduplicates_same_normalized_title_date_and_unit() -> None:
    first_url = "https://ccs.nptu.edu.tw/p/406-1025-200010.php?Lang=zh-tw"
    second_url = "https://ccs.nptu.edu.tw/p/406-1025-200011.php?Lang=zh-tw"
    html = _listing(
        ("2026-08-05", first_url, "【獎助學金】ＡＢＣ　獎學金"),
        ("2026-08-05", second_url, "【獎助學金】ABC 獎學金"),
    )

    items = _adapter().parse_listing(html)

    assert len(items) == 1
    assert items[0].canonical_url == first_url


def test_listing_keeps_same_title_on_different_dates() -> None:
    first_url = "https://ccs.nptu.edu.tw/p/406-1025-200020.php?Lang=zh-tw"
    second_url = "https://ccs.nptu.edu.tw/p/406-1025-200021.php?Lang=zh-tw"
    html = _listing(
        ("2026-08-05", first_url, "例行公告"),
        ("2026-08-04", second_url, "例行公告"),
    )

    items = _adapter().parse_listing(html)

    assert [item.canonical_url for item in items] == [first_url, second_url]
