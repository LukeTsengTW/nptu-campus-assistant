from __future__ import annotations

from datetime import date

from nptu_assistant.crawlers.adapters.nptu import NptuOverviewAdapter
from nptu_assistant.crawlers.parsing import parse_deadline, parse_published_at


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss><channel><item>
  <title><![CDATA[115學年度申請公告]]></title>
  <link><![CDATA[https://academic.nptu.edu.tw/p/406-1.php]]></link>
  <description><![CDATA[<p>申請截止日：2026年7月31日</p>]]></description>
  <pubDate>2026-07-10 16:00:00</pubDate>
  <author><![CDATA[教務處]]></author>
</item></channel></rss>"""


DETAIL_FIXTURE = """
<html><body><nav>導覽</nav><main class="main"><h1>115學年度申請公告</h1>
<div class="meditor"><p>這是完整公告。</p><script>bad()</script></div></main></body></html>
"""


def test_nptu_adapter_parses_feed_and_detail() -> None:
    adapter = NptuOverviewAdapter()

    items = adapter.parse_listing(RSS_FIXTURE)
    detail = adapter.parse_detail(DETAIL_FIXTURE)

    assert len(items) == 1
    assert items[0].title == "115學年度申請公告"
    assert items[0].unit == "教務處"
    assert items[0].published_at == date(2026, 7, 10)
    assert items[0].deadline_at == date(2026, 7, 31)
    assert detail == "115學年度申請公告\n這是完整公告。"


def test_date_parsing_supports_iso_and_roc_year() -> None:
    assert parse_published_at("2026-07-10 09:30:00") == date(2026, 7, 10)
    assert parse_published_at("115年7月10日") == date(2026, 7, 10)


def test_deadline_parser_returns_none_without_explicit_signal() -> None:
    assert parse_deadline("活動日期為 2026年7月31日") is None
    assert parse_deadline("報名截止日：115年7月31日") == date(2026, 7, 31)


def test_nptu_adapter_skips_non_allowlisted_links() -> None:
    xml = RSS_FIXTURE.replace(
        "https://academic.nptu.edu.tw/p/406-1.php", "https://example.com/phishing"
    )

    assert NptuOverviewAdapter().parse_listing(xml) == []


def test_nptu_adapter_skips_malformed_item_without_losing_valid_items() -> None:
    invalid = """<item><title></title><link>https://www.nptu.edu.tw/bad</link>
    <description>bad</description><pubDate></pubDate></item>"""
    xml = RSS_FIXTURE.replace("<item>", invalid + "<item>", 1)

    items = NptuOverviewAdapter().parse_listing(xml)

    assert len(items) == 1
    assert items[0].canonical_url == "https://academic.nptu.edu.tw/p/406-1.php"
