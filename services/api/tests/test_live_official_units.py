from __future__ import annotations

import os
from pathlib import Path

import pytest

from nptu_assistant.crawlers.adapters.factory import build_adapter
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePageAdapter
from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.official_units import load_official_unit_directory
from nptu_assistant.crawlers.site_models import SearchPlan
from nptu_assistant.crawlers.site_search import NptuSiteSearchService


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_NPTU_LIVE_SMOKE") != "1",
    reason="requires explicit NPTU live smoke opt-in",
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
ANNOUNCEMENT_CONFIG = WORKSPACE_ROOT / "data/sources/announcements.yaml"
UNIT_CONFIG = WORKSPACE_ROOT / "data/sources/official_units.yaml"


@pytest.mark.parametrize(
    "canonical_name",
    ["資訊學院", "企業管理學系", "教育學系", "中國語文學系", "應用化學系"],
)
def test_live_representative_unit_homepage_and_scoped_search(
    canonical_name: str,
) -> None:
    directory = load_official_unit_directory(UNIT_CONFIG)
    unit = directory.get(canonical_name)
    assert unit is not None
    assert unit.homepage_url is not None
    client = CrawlHttpClient(
        "NPTU-Campus-Assistant-Official-Unit-Smoke/0.1",
        interval_seconds=1,
    )
    try:
        html = client.get_html(unit.homepage_url, allowed_hosts=unit.allowed_hosts)
        page = NptuSitePageAdapter().parse_page(
            html,
            unit.homepage_url,
            allowed_hosts=unit.allowed_hosts,
        )
        assert page.body
        assert any(host in page.canonical_url for host in unit.allowed_hosts)

        if unit.announcement_strategy.value == "scoped_site_search":
            base = load_keyword_search_config(ANNOUNCEMENT_CONFIG).site_search
            assert base is not None
            config = base.model_copy(
                update={
                    "max_pages": 5,
                    "max_items": 2,
                    "max_candidate_urls": 10,
                    "early_stop_min_results": 1,
                }
            )
            result = NptuSiteSearchService(config, client).search(
                SearchPlan.from_query(f"{canonical_name} 最新公告", limit=2),
                max_items=2,
                use_discovery=False,
                scope=directory.scope_for(unit),
            )
            assert result.visited_count >= 1
    finally:
        client.close()


def test_live_configured_listing_parses_at_least_one_item() -> None:
    source = next(
        item
        for item in load_source_configs(ANNOUNCEMENT_CONFIG)
        if item.name == "information-college-html"
    )
    client = CrawlHttpClient(
        "NPTU-Campus-Assistant-Official-Unit-Smoke/0.1",
        interval_seconds=1,
    )
    try:
        html = client.get(source.url, allowed_hosts=source.allowed_hosts)
        items = build_adapter(source).parse_listing(html)
    finally:
        client.close()

    assert items
    assert all(item.unit == "資訊學院" for item in items)
