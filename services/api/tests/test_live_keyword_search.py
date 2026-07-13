from __future__ import annotations

import os
from pathlib import Path

import pytest

from nptu_assistant.crawlers.adapters.nptu_search import NptuAssociationSearchAdapter
from nptu_assistant.crawlers.config import load_keyword_search_config
from nptu_assistant.crawlers.http import CrawlHttpClient


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_KEYWORD_SEARCH") != "1",
    reason="requires explicit live NPTU crawler opt-in",
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def test_live_keyword_search_form_and_result_contract() -> None:
    config = load_keyword_search_config(WORKSPACE_ROOT / "data/sources/announcements.yaml")
    adapter = NptuAssociationSearchAdapter()
    client = CrawlHttpClient("NPTU-Campus-Assistant-Live-Smoke/0.1", interval_seconds=1)
    try:
        client.get(config.session_url)
        bootstrap = adapter.parse_bootstrap_form(
            client.submit_form(config.bootstrap_method, config.bootstrap_url, {}),
            config.bootstrap_url,
        )
        fields = dict(bootstrap.hidden_fields)
        fields.update({"SchKey": "電科系", "SchType": "part"})
        content = client.submit_form(bootstrap.method, config.url, fields)
        form = adapter.parse_form(content, config.url)
        results = adapter.parse_results(
            content,
            config.url,
        )
    finally:
        client.close()

    assert "part" in form.search_types
    assert all(result.canonical_url.startswith("https://") for result in results)
