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


def test_live_information_college_listing_contract_without_database_writes() -> None:
    config = next(
        item
        for item in load_source_configs(CONFIG_PATH)
        if item.name == "information-college-html"
    )
    client = CrawlHttpClient(
        "NPTU-Campus-Assistant-Unit-Source-Smoke/0.1",
        interval_seconds=1,
    )
    try:
        content = client.get(config.url, allowed_hosts=config.allowed_hosts)
        items = build_adapter(config).parse_listing(content)
    finally:
        client.close()

    assert items
    assert len(items) <= config.max_items
    assert all(item.unit == "資訊學院" for item in items)
    assert all(item.canonical_url.startswith("https://ccs.nptu.edu.tw/") for item in items)
