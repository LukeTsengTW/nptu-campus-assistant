from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.official_units import (
    load_official_unit_directory_for_config,
)
from nptu_assistant.crawlers.refresh import RefreshResult
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.rag.service import ChatService


CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "sources" / "announcements.yaml"
)


class _RecordingRefresher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        self.calls.append(source_name)
        return RefreshResult(
            source_name=source_name,
            attempted=True,
            succeeded=True,
            canonical_urls=("https://www.nptu.edu.tw/p/406-1000-200001.php",),
        )


def _resolver() -> UnitSourceResolver:
    keyword_config = load_keyword_search_config(CONFIG_PATH)
    return UnitSourceResolver(
        load_source_configs(CONFIG_PATH),
        keyword_config.aliases,
        keyword_config.source_routes,
        load_official_unit_directory_for_config(CONFIG_PATH),
    )


def _service(refresher: _RecordingRefresher) -> ChatService:
    return ChatService(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        announcement_refresher=refresher,
        unit_source_resolver=_resolver(),
    )


@pytest.mark.parametrize(
    ("question", "expected_calls"),
    [
        ("查詢近期最新公告 ", ["nptu-overview"]),
        ("查詢近期最新獎學金公告", []),
        ("資訊學院近期最新公告", []),
    ],
)
def test_latest_announcement_preflight_only_refreshes_global_unfiltered_queries(
    question: str,
    expected_calls: list[str],
) -> None:
    refresher = _RecordingRefresher()

    warning = _service(refresher)._refresh_global_latest_announcements(question)

    assert warning is None
    assert refresher.calls == expected_calls
