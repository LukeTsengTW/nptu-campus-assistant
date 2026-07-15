from __future__ import annotations

import os
from pathlib import Path

import pytest

from nptu_assistant.crawlers.adapters.factory import build_adapter
from nptu_assistant.crawlers.config import load_source_configs
from nptu_assistant.crawlers.http import CrawlHttpClient


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_UNIT_ANNOUNCEMENT_SOURCE") != "1",
    reason="requires explicit live unit announcement source opt-in",
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"


@pytest.mark.parametrize(
    ("source_name", "expected_unit", "expected_title_term"),
    [
        ("student-scholarship-external-html", "生活輔導組", "獎學金"),
        ("student-scholarship-internal-html", "生活輔導組", "校內獎學金"),
    ],
)
def test_live_scholarship_listing_contract_without_database_writes(
    source_name: str,
    expected_unit: str,
    expected_title_term: str,
) -> None:
    config = next(
        item
        for item in load_source_configs(CONFIG_PATH)
        if item.name == source_name
    )
    client = CrawlHttpClient(
        "NPTU-Campus-Assistant-Unit-Source-Smoke/0.1",
        interval_seconds=1,
    )
    try:
        page = client.get(config.url, allowed_hosts=config.allowed_hosts)
        assert config.selectors is not None
        assert config.selectors.listing in page
        assert config.dynamic_listing is not None
        fragment = client.submit_form(
            config.dynamic_listing.method,
            config.dynamic_listing.url,
            {},
            allowed_hosts=config.allowed_hosts,
        )
        content = (
            f'<div id="{config.dynamic_listing.wrapper_id}">{fragment}</div>'
        )
        items = build_adapter(config).parse_listing(content)
    finally:
        client.close()

    assert items
    assert len(items) <= config.max_items
    assert all(item.unit == expected_unit for item in items)
    assert all(item.canonical_url.startswith("https://staf-life.nptu.edu.tw/") for item in items)
    assert any(expected_title_term in item.title for item in items)
