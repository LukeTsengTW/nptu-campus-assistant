from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any, cast

import pytest

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.official_units import (
    load_official_unit_directory_for_config,
)
from nptu_assistant.crawlers.refresh import RefreshResult
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.crawlers.site_models import SearchDeadline
from nptu_assistant.rag.completeness import (
    CompletenessConfig,
    CompletenessFacts,
    CompletenessMode,
    DbFirstCompletenessPolicy,
)
from nptu_assistant.rag.models import (
    ConversationContext,
    Evidence,
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)
from nptu_assistant.rag.service import ChatService
from nptu_assistant.rag.tools import AnnouncementSort


CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "sources" / "announcements.yaml"
)
LATEST_URL = "https://www.nptu.edu.tw/p/406-1000-200001.php"
SCHOLARSHIP_URL = (
    "https://staf-life.nptu.edu.tw/p/406-1074-198126,r3893.php?Lang=zh-tw"
)


class _RecordingRefresher:
    def __init__(
        self,
        *,
        succeeded: bool = True,
        warning: str | None = None,
    ) -> None:
        self.succeeded = succeeded
        self.warning = warning
        self.calls: list[str] = []

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        self.calls.append(source_name)
        return RefreshResult(
            source_name=source_name,
            attempted=True,
            succeeded=self.succeeded,
            warning=self.warning,
            canonical_urls=(LATEST_URL,),
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
    ("question", "expected_calls", "snapshot_ready"),
    [
        ("查詢近期最新公告 ", ["nptu-overview"], True),
        (
            "查詢近期最新獎學金公告",
            ["student-scholarship-external-html"],
            True,
        ),
        (
            "查詢校內獎學金公告",
            ["student-scholarship-internal-html"],
            True,
        ),
        ("資訊學院近期最新公告", [], False),
        ("獎學金申請資格", [], False),
    ],
)
def test_latest_announcement_preflight_refreshes_authoritative_listing_sources(
    question: str,
    expected_calls: list[str],
    snapshot_ready: bool,
) -> None:
    refresher = _RecordingRefresher()

    result = _service(refresher)._refresh_latest_announcements(question)

    assert result.snapshot_ready is snapshot_ready
    assert result.warning is None
    assert refresher.calls == expected_calls


def test_failed_latest_preflight_preserves_refresh_warning() -> None:
    warning = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    refresher = _RecordingRefresher(succeeded=False, warning=warning)

    result = _service(refresher)._refresh_latest_announcements("查詢近期最新公告")

    assert result.snapshot_ready is False
    assert result.warning == warning
    assert refresher.calls == ["nptu-overview"]


class _Retriever:
    def __init__(self, item: Evidence) -> None:
        self.item = item
        self.calls: list[tuple[object, ...]] = []

    def search_announcements(
        self,
        *,
        query: str | None,
        limit: int,
        sort: AnnouncementSort,
        unit: str | None,
        date_from: date | None,
        date_to: date | None,
        canonical_urls: tuple[str, ...] | None = None,
    ) -> list[Evidence]:
        self.calls.append(
            (query, limit, sort, unit, date_from, date_to, canonical_urls)
        )
        return [self.item]

    def search_documents(self, *, query: str, limit: int) -> list[Evidence]:
        del query, limit
        return []

    def search_documents_with_plan(
        self,
        *,
        plan: object,
        limit: int,
        deadline: object | None = None,
    ) -> list[Evidence]:
        del plan, limit, deadline
        return []

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        del announcement_id
        return None


class _ScriptedProvider:
    def __init__(self, item: Evidence, *, query: str | None = None) -> None:
        self.item = item
        self.query = query
        self.inputs: list[list[dict[str, object]]] = []
        self.turn = 0

    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        del instructions, tools
        self.inputs.append(list(input_items))
        self.turn += 1
        if self.turn == 1:
            arguments = json.dumps(
                {
                    "query": self.query,
                    "limit": 5,
                    "sort": "newest",
                    "unit": None,
                    "date_from": None,
                    "date_to": None,
                },
                ensure_ascii=False,
            )
            item = {
                "type": "function_call",
                "call_id": "call-latest",
                "name": "search_announcements",
                "arguments": arguments,
            }
            return ModelTurn(
                output_items=[item],
                tool_calls=[ToolCall("call-latest", "search_announcements", arguments)],
            )
        return ModelTurn(
            output_items=[{"type": "message", "role": "assistant"}],
            generated=GeneratedAnswer(
                "以下為近期最新公告。",
                [self.item.id],
                response_kind=ResponseKind.GROUNDED,
            ),
        )


class _ConversationStore:
    def load_or_create(self, conversation_id: str | None) -> ConversationContext:
        del conversation_id
        return ConversationContext("conversation-latest", [], [])

    def save_turn(self, **kwargs: object) -> None:
        del kwargs

    def delete(self, conversation_id: str) -> bool:
        del conversation_id
        return True


class _WeakAnnouncementFacts:
    def announcement_facts(self, *args: object, **kwargs: object) -> CompletenessFacts:
        del args, kwargs
        return CompletenessFacts(
            evidence_count=1,
            unique_url_count=1,
            strong_evidence_count=0,
            current_document_count=1,
            fresh_count=1,
            source_coverage_ratio=0.0,
            canonical_urls=(LATEST_URL,),
            source_names=("nptu-overview",),
        )


class _RecordingKeywordIngestor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def normalize(self, text: str) -> str:
        return text

    def ingest(self, query: str, **kwargs: object) -> Any:
        del kwargs
        self.calls.append(query)
        raise AssertionError(
            "successful listing preflight must skip keyword live fallback"
        )


class _DeadlineSiteIngestor:
    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(8.0)


def test_successful_overview_preflight_skips_redundant_live_fallback_and_warning() -> (
    None
):
    item = Evidence(
        id="latest-announcement",
        kind=AnswerType.ANNOUNCEMENT,
        title="2026-08-05 最新公告",
        url=LATEST_URL,
        unit="國立屏東大學",
        published_at=date(2026, 8, 5),
        content="2026-08-05 最新公告內容",
        score=0.9,
    )
    refresher = _RecordingRefresher()
    keyword_ingestor = _RecordingKeywordIngestor()
    provider = _ScriptedProvider(item)

    response = ChatService(
        _Retriever(item),
        provider,
        _ConversationStore(),
        announcement_refresher=refresher,
        keyword_announcement_ingestor=keyword_ingestor,
        unit_source_resolver=_resolver(),
        site_page_ingestor=cast(Any, _DeadlineSiteIngestor()),
        completeness_policy=DbFirstCompletenessPolicy(
            CompletenessConfig(rollout_mode=CompletenessMode.ENFORCE)
        ),
        completeness_facts=cast(Any, _WeakAnnouncementFacts()),
        live_fallback_max_seconds=8.0,
    ).answer("查詢近期最新公告")

    assert refresher.calls == ["nptu-overview"]
    assert keyword_ingestor.calls == []
    assert response.warning is None
    assert response.sources[0].url == LATEST_URL
    tool_output = json.loads(provider.inputs[1][-1]["output"])
    assert tool_output["warning"] is None


def test_successful_scholarship_preflight_skips_redundant_live_fallback_and_warning() -> (
    None
):
    item = Evidence(
        id="latest-scholarship",
        kind=AnswerType.ANNOUNCEMENT,
        title="115-1 財團法人得力教育基金會清寒獎助學金",
        url=SCHOLARSHIP_URL,
        unit="生活輔導組",
        published_at=date(2026, 8, 5),
        content="115學年度第1學期清寒獎助學金公告內容",
        score=0.9,
    )
    refresher = _RecordingRefresher()
    keyword_ingestor = _RecordingKeywordIngestor()
    provider = _ScriptedProvider(item, query="獎學金公告")

    response = ChatService(
        _Retriever(item),
        provider,
        _ConversationStore(),
        announcement_refresher=refresher,
        keyword_announcement_ingestor=keyword_ingestor,
        unit_source_resolver=_resolver(),
        site_page_ingestor=cast(Any, _DeadlineSiteIngestor()),
        completeness_policy=DbFirstCompletenessPolicy(
            CompletenessConfig(rollout_mode=CompletenessMode.ENFORCE)
        ),
        completeness_facts=cast(Any, _WeakAnnouncementFacts()),
        live_fallback_max_seconds=8.0,
    ).answer("查詢獎學金公告")

    assert refresher.calls == ["student-scholarship-external-html"]
    assert keyword_ingestor.calls == []
    assert response.warning is None
    assert response.sources[0].url == SCHOLARSHIP_URL
    tool_output = json.loads(provider.inputs[1][-1]["output"])
    assert tool_output["warning"] is None
