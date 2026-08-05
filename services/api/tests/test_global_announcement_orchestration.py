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
from nptu_assistant.crawlers.search import PARTIAL_SEARCH_FAILURE_WARNING
from nptu_assistant.crawlers.site_models import SearchDeadline
from nptu_assistant.rag.completeness import (
    CompletenessConfig,
    CompletenessFacts,
    CompletenessMode,
    DbFirstCompletenessPolicy,
)
from nptu_assistant.rag.completeness_refresh import RefreshScheduleResult
from nptu_assistant.rag.models import (
    ConversationContext,
    Evidence,
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)
from nptu_assistant.rag.service import ChatService
from nptu_assistant.rag.tools import (
    AnnouncementSort,
    DB_REFRESH_SCHEDULED_WARNING,
)


CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "sources" / "announcements.yaml"
)
ITEM_URL = "https://www.nptu.edu.tw/p/406-1000-200001.php"


class _RecordingRefresher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        self.calls.append(source_name)
        return RefreshResult(
            source_name=source_name,
            attempted=True,
            succeeded=True,
            canonical_urls=(ITEM_URL,),
        )


def _resolver() -> UnitSourceResolver:
    keyword_config = load_keyword_search_config(CONFIG_PATH)
    return UnitSourceResolver(
        load_source_configs(CONFIG_PATH),
        keyword_config.aliases,
        keyword_config.source_routes,
        load_official_unit_directory_for_config(CONFIG_PATH),
    )


def _arguments(query: str | None, *, unit: str | None = None) -> str:
    return json.dumps(
        {
            "query": query,
            "limit": 5,
            "sort": "newest",
            "unit": unit,
            "date_from": None,
            "date_to": None,
        },
        ensure_ascii=False,
    )


@pytest.mark.parametrize(
    ("arguments", "expected_source"),
    [
        (_arguments(None), "nptu-overview"),
        (_arguments("獎學金公告"), "student-scholarship-external-html"),
        (_arguments("校內獎學金公告"), "student-scholarship-internal-html"),
        (_arguments("社團活動"), None),
    ],
)
def test_preflight_uses_actual_tool_arguments_instead_of_prompt_phrases(
    arguments: str,
    expected_source: str | None,
) -> None:
    refresher = _RecordingRefresher()
    service = ChatService(
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, object()),
        announcement_refresher=refresher,
        unit_source_resolver=_resolver(),
        completeness_policy=DbFirstCompletenessPolicy(
            CompletenessConfig(rollout_mode=CompletenessMode.ENFORCE)
        ),
    )

    result = service._refresh_announcement_call(arguments)

    assert result.snapshot_ready is (expected_source is not None)
    assert refresher.calls == ([] if expected_source is None else [expected_source])


class _Retriever:
    def __init__(self, item: Evidence) -> None:
        self.item = item

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
        del query, limit, sort, unit, date_from, date_to, canonical_urls
        return [self.item]

    def search_documents(self, *, query: str, limit: int) -> list[Evidence]:
        del query, limit
        return []

    def search_documents_with_plan(self, **kwargs: object) -> list[Evidence]:
        del kwargs
        return []

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        del announcement_id
        return None


class _Provider:
    def __init__(self, item: Evidence) -> None:
        self.item = item
        self.turn = 0
        self.inputs: list[list[dict[str, object]]] = []

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
            arguments = _arguments("社團活動")
            return ModelTurn(
                output_items=[
                    {
                        "type": "function_call",
                        "call_id": "call-activity",
                        "name": "search_announcements",
                        "arguments": arguments,
                    }
                ],
                tool_calls=[
                    ToolCall("call-activity", "search_announcements", arguments)
                ],
            )
        return ModelTurn(
            output_items=[{"type": "message", "role": "assistant"}],
            generated=GeneratedAnswer(
                "以下為相關活動公告。",
                [self.item.id],
                response_kind=ResponseKind.GROUNDED,
            ),
        )


class _Store:
    def load_or_create(self, conversation_id: str | None) -> ConversationContext:
        del conversation_id
        return ConversationContext("conversation-global", [], [])

    def save_turn(self, **kwargs: object) -> None:
        del kwargs

    def delete(self, conversation_id: str) -> bool:
        del conversation_id
        return True


class _WeakFreshFacts:
    def announcement_facts(self, *args: object, **kwargs: object) -> CompletenessFacts:
        del args, kwargs
        return CompletenessFacts(
            evidence_count=1,
            unique_url_count=1,
            strong_evidence_count=0,
            top_score=0.2,
            score_margin=0.0,
            current_document_count=1,
            fresh_count=1,
            source_coverage_ratio=1.0,
            canonical_urls=(ITEM_URL,),
            source_names=("nptu-overview",),
        )


class _NoLiveKeywordIngestor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def normalize(self, text: str) -> str:
        return text

    def ingest(self, query: str, **kwargs: object) -> Any:
        del kwargs
        self.calls.append(query)
        raise AssertionError(
            "fresh database evidence must not use synchronous live search"
        )


class _DeadlineSiteIngestor:
    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(8.0)


class _RefreshScheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def schedule(self, **kwargs: object) -> RefreshScheduleResult:
        self.calls.append(dict(kwargs))
        return RefreshScheduleResult(
            attempted=True,
            succeeded=True,
            target_count=2,
            scheduled_count=2,
            reason="weak_but_usable_announcement_evidence",
        )


def test_arbitrary_keyword_prompt_uses_db_and_background_refresh_without_false_warning() -> (
    None
):
    item = Evidence(
        id="activity-announcement",
        kind=AnswerType.ANNOUNCEMENT,
        title="學生社團活動公告",
        url=ITEM_URL,
        unit="學生活動發展組",
        published_at=date(2026, 8, 5),
        content="學生社團活動公告內容",
        score=0.2,
    )
    provider = _Provider(item)
    keyword_ingestor = _NoLiveKeywordIngestor()
    scheduler = _RefreshScheduler()

    response = ChatService(
        _Retriever(item),
        provider,
        _Store(),
        keyword_announcement_ingestor=keyword_ingestor,
        unit_source_resolver=_resolver(),
        site_page_ingestor=cast(Any, _DeadlineSiteIngestor()),
        completeness_policy=DbFirstCompletenessPolicy(
            CompletenessConfig(rollout_mode=CompletenessMode.ENFORCE)
        ),
        completeness_facts=cast(Any, _WeakFreshFacts()),
        refresh_scheduler=cast(Any, scheduler),
        live_fallback_max_seconds=8.0,
    ).answer("幫我看看最近有沒有適合學生參加的活動")

    assert keyword_ingestor.calls == []
    assert len(scheduler.calls) == 1
    assert response.warning == DB_REFRESH_SCHEDULED_WARNING
    assert PARTIAL_SEARCH_FAILURE_WARNING not in response.warning
    tool_output = json.loads(provider.inputs[1][-1]["output"])
    assert tool_output["warning"] == DB_REFRESH_SCHEDULED_WARNING
