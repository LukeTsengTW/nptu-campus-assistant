from __future__ import annotations

from nptu_assistant.crawlers.adapters.base import CrawlerAdapter
from nptu_assistant.crawlers.adapters.fixture import FixtureAdapter
from nptu_assistant.crawlers.adapters.nptu import NptuOverviewAdapter
from nptu_assistant.crawlers.adapters.nptu_html import NptuHtmlListAdapter
from nptu_assistant.crawlers.config import CrawlerSourceConfig


def build_adapter(config: CrawlerSourceConfig) -> CrawlerAdapter:
    if config.adapter == "fixture":
        return FixtureAdapter()
    if config.adapter == "nptu_overview":
        return NptuOverviewAdapter()
    if config.adapter == "nptu_html_list":
        return NptuHtmlListAdapter(config)
    raise ValueError(f"未支援的 adapter：{config.adapter}")
