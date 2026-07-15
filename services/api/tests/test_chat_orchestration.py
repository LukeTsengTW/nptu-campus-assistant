from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.crawlers.refresh import RefreshResult
from nptu_assistant.crawlers.config import load_keyword_search_config, load_source_configs
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.providers.fake import FakeLlmProvider
from nptu_assistant.rag.models import (
    ConversationContext,
    Evidence,
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)
from nptu_assistant.rag.service import ChatService, INSUFFICIENT_ANSWER
from nptu_assistant.rag.tools import AnnouncementSort, ToolExecutor


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"


def evidence(
    source_id: str = "announcement-1",
    *,
    kind: AnswerType = AnswerType.ANNOUNCEMENT,
) -> Evidence:
    return Evidence(
        id=source_id,
        kind=kind,
        title="測試公告" if kind is AnswerType.ANNOUNCEMENT else "測試文件",
        url=f"https://www.nptu.edu.tw/source/{source_id}",
        unit="教務處",
        published_at=date(2026, 7, 12),
        content="正式資料內容",
        score=0.8,
    )


class StubRetriever:
    def __init__(self, by_tool: dict[str, list[Evidence] | Evidence | None]) -> None:
        self.by_tool = by_tool
        self.calls: list[tuple[str, object]] = []

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
            (
                "search_announcements",
                (query, limit, sort, unit, date_from, date_to, canonical_urls),
            )
        )
        return list(self.by_tool.get("search_announcements", []))  # type: ignore[arg-type]

    def search_documents(self, *, query: str, limit: int) -> list[Evidence]:
        self.calls.append(("search_documents", (query, limit)))
        return list(self.by_tool.get("search_documents", []))  # type: ignore[arg-type]

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        self.calls.append(("get_announcement", announcement_id))
        value = self.by_tool.get("get_announcement")
        return value if isinstance(value, Evidence) else None


class ScriptedProvider:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.inputs: list[list[dict[str, object]]] = []

    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        assert "繁體中文" in instructions
        assert {tool["name"] for tool in tools} == {
            "search_announcements",
            "search_documents",
            "get_announcement",
        }
        self.inputs.append(list(input_items))
        return self.turns.pop(0)


class StubConversationStore:
    def __init__(self, context: ConversationContext | None = None) -> None:
        self.context = context or ConversationContext("conversation-1", [], [])
        self.saved: dict[str, object] | None = None

    def load_or_create(self, conversation_id: str | None) -> ConversationContext:
        del conversation_id
        return self.context

    def save_turn(self, **kwargs: object) -> None:
        self.saved = kwargs


class RecordingRefresher:
    def __init__(
        self,
        warning: str | None = None,
        canonical_urls: tuple[str, ...] | None = (
            "https://ccs.nptu.edu.tw/p/406-1025-197412,r1019.php?Lang=zh-tw",
        ),
    ) -> None:
        self.warning = warning
        self.canonical_urls = canonical_urls
        self.calls: list[str] = []

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        self.calls.append(source_name)
        return RefreshResult(
            source_name=source_name,
            attempted=True,
            succeeded=self.warning is None,
            warning=self.warning,
            canonical_urls=self.canonical_urls,
        )


def project_unit_resolver() -> UnitSourceResolver:
    keyword_config = load_keyword_search_config(CONFIG_PATH)
    return UnitSourceResolver(
        load_source_configs(CONFIG_PATH),
        keyword_config.aliases,
        keyword_config.source_routes,
    )


def function_turn(call_id: str, name: str, arguments: dict[str, object]) -> ModelTurn:
    item = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments, ensure_ascii=False),
    }
    return ModelTurn(
        output_items=[item],
        tool_calls=[ToolCall(call_id, name, item["arguments"])],
    )


def final_turn(
    answer: str,
    source_ids: list[str],
    *,
    kind: ResponseKind = ResponseKind.GROUNDED,
) -> ModelTurn:
    return ModelTurn(
        output_items=[{"type": "message", "role": "assistant"}],
        generated=GeneratedAnswer(answer, source_ids, response_kind=kind),
    )


def announcement_args() -> dict[str, object]:
    return {
        "query": None,
        "limit": 5,
        "sort": "newest",
        "unit": None,
        "date_from": None,
        "date_to": None,
    }


def test_chat_executes_tool_and_returns_backend_allowlisted_sources() -> None:
    item = evidence()
    provider = ScriptedProvider(
        [
            function_turn("call-1", "search_announcements", announcement_args()),
            final_turn("以下是最近公告。", [item.id]),
        ]
    )
    store = StubConversationStore()

    response = ChatService(StubRetriever({"search_announcements": [item]}), provider, store).answer(
        "最近公告"
    )

    assert response.conversation_id == "conversation-1"
    assert response.sources[0].id == item.id
    assert response.sources[0].kind == "announcement"
    assert response.sources[0].url == item.url
    function_output = provider.inputs[1][-1]
    assert function_output["type"] == "function_call_output"
    assert function_output["call_id"] == "call-1"
    assert json.loads(function_output["output"])["count"] == 1
    assert store.saved is not None


def test_newest_announcement_tool_refreshes_before_database_search() -> None:
    retriever = StubRetriever({"search_announcements": [evidence()]})
    refresher = RecordingRefresher()
    executor = ToolExecutor(retriever, refresher)

    result = executor.execute("search_announcements", json.dumps(announcement_args()))

    assert refresher.calls == ["nptu-overview"]
    assert len(result.evidence) == 1


def test_non_newest_announcement_tool_does_not_request_refresh() -> None:
    arguments = announcement_args()
    arguments["sort"] = "relevance"
    refresher = RecordingRefresher()
    executor = ToolExecutor(StubRetriever({"search_announcements": []}), refresher)

    executor.execute("search_announcements", json.dumps(arguments))

    assert refresher.calls == []


def test_refresh_failure_keeps_database_evidence_and_returns_warning() -> None:
    warning = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    executor = ToolExecutor(
        StubRetriever({"search_announcements": [evidence()]}),
        RecordingRefresher(warning),
    )

    result = executor.execute("search_announcements", json.dumps(announcement_args()))

    assert len(result.evidence) == 1
    assert result.warning == warning
    assert json.loads(result.output)["warning"] == warning


def test_chat_surfaces_refresh_warning_with_database_sources() -> None:
    item = evidence()
    warning = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    provider = ScriptedProvider(
        [
            function_turn("call-1", "search_announcements", announcement_args()),
            final_turn("依據資料庫中的公告內容。", [item.id]),
        ]
    )

    response = ChatService(
        StubRetriever({"search_announcements": [item]}),
        provider,
        StubConversationStore(),
        announcement_refresher=RecordingRefresher(warning),
    ).answer("幫我查最新公告")

    assert response.answer == "依據資料庫中的公告內容。"
    assert response.warning == warning
    assert response.sources[0].url == item.url


def test_refresh_failure_without_database_evidence_remains_insufficient() -> None:
    warning = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    provider = ScriptedProvider(
        [
            function_turn("call-1", "search_announcements", announcement_args()),
            final_turn("目前收錄的官方資料不足以確認。", [], kind=ResponseKind.INSUFFICIENT),
        ]
    )

    response = ChatService(
        StubRetriever({"search_announcements": []}),
        provider,
        StubConversationStore(),
        announcement_refresher=RecordingRefresher(warning),
    ).answer("幫我查最新公告")

    assert response.answer == INSUFFICIENT_ANSWER
    assert response.answer_type is AnswerType.INSUFFICIENT_INFORMATION
    assert response.sources == []


def test_chat_supports_parallel_tools_in_one_model_turn() -> None:
    announcement = evidence("announcement-1")
    document = evidence("document-1", kind=AnswerType.OFFICIAL_DOCUMENT)
    first = ModelTurn(
        output_items=[
            {"type": "function_call", "call_id": "a", "name": "search_announcements", "arguments": json.dumps(announcement_args())},
            {"type": "function_call", "call_id": "d", "name": "search_documents", "arguments": json.dumps({"query": "學貸", "limit": 6})},
        ],
        tool_calls=[
            ToolCall("a", "search_announcements", json.dumps(announcement_args())),
            ToolCall("d", "search_documents", json.dumps({"query": "學貸", "limit": 6})),
        ],
    )
    provider = ScriptedProvider([first, final_turn("公告與流程如下。", [announcement.id, document.id])])
    retriever = StubRetriever(
        {"search_announcements": [announcement], "search_documents": [document]}
    )

    response = ChatService(retriever, provider, StubConversationStore()).answer("學貸公告與流程")

    assert [source.id for source in response.sources] == [announcement.id, document.id]
    assert [call[0] for call in retriever.calls] == ["search_announcements", "search_documents"]


def test_chat_rejects_unknown_tool_without_executing_retriever() -> None:
    provider = ScriptedProvider(
        [
            function_turn("bad", "drop_database", {}),
            final_turn("目前無法完成。", [], kind=ResponseKind.INSUFFICIENT),
        ]
    )
    retriever = StubRetriever({})

    ChatService(retriever, provider, StubConversationStore()).answer("測試")

    output = json.loads(provider.inputs[1][-1]["output"])
    assert output["error"]["code"] == "unknown_tool"
    assert retriever.calls == []


def test_chat_stops_after_four_tool_rounds() -> None:
    turns = [function_turn(f"call-{index}", "search_announcements", announcement_args()) for index in range(5)]
    service = ChatService(
        StubRetriever({"search_announcements": []}),
        ScriptedProvider(turns),
        StubConversationStore(),
    )

    with pytest.raises(AppError) as error:
        service.answer("反覆查詢")

    assert error.value.code == "tool_round_limit"


def test_chat_sanitizes_unknown_ids_urls_and_internal_uuids() -> None:
    item = evidence()
    internal_id = "394a51a1-c0fc-4b96-a81f-f2acd9bd46e4"
    provider = ScriptedProvider(
        [
            function_turn("call-1", "search_announcements", announcement_args()),
            final_turn(
                f"來源 {item.url}，偽造 https://example.com/x，內部 {internal_id}",
                [item.id, "unknown-id"],
            ),
        ]
    )

    response = ChatService(
        StubRetriever({"search_announcements": [item]}),
        provider,
        StubConversationStore(),
    ).answer("最近公告")

    assert item.url in response.answer
    assert "example.com" not in response.answer
    assert internal_id not in response.answer
    assert [source.id for source in response.sources] == [item.id]


def test_grounded_answer_without_valid_sources_becomes_insufficient() -> None:
    response = ChatService(
        StubRetriever({}),
        ScriptedProvider([final_turn("模型自行回答", ["unknown"])]),
        StubConversationStore(),
    ).answer("測試")

    assert response.answer == INSUFFICIENT_ANSWER
    assert response.sources == []


def test_clarification_can_return_without_sources() -> None:
    response = ChatService(
        StubRetriever({}),
        ScriptedProvider([final_turn("請問你指的是哪一類的前五個？", [], kind=ResponseKind.CLARIFICATION)]),
        StubConversationStore(),
    ).answer("前五個")

    assert response.answer == "請問你指的是哪一類的前五個？"
    assert response.answer_type is AnswerType.INSUFFICIENT_INFORMATION
    assert response.sources == []


def test_follow_up_uses_context_announcement_id_with_get_announcement() -> None:
    summary = evidence("announcement-3")
    detail = replace(summary, content="完整詳細內容", score=1.0)
    context = ConversationContext(
        "conversation-1",
        [
            {"role": "user", "content": "列出最近五則公告"},
            {"role": "assistant", "content": "第三則是測試公告"},
        ],
        [summary],
    )
    provider = ScriptedProvider(
        [
            function_turn(
                "detail",
                "get_announcement",
                {"announcement_id": "announcement-3"},
            ),
            final_turn("完整詳細內容", ["announcement-3"]),
        ]
    )
    retriever = StubRetriever({"get_announcement": detail})

    response = ChatService(
        retriever,
        provider,
        StubConversationStore(context),
    ).answer("詳細說明那一則", "conversation-1")

    assert retriever.calls == [("get_announcement", "announcement-3")]
    assert response.sources[0].id == "announcement-3"
    assert response.answer == "完整詳細內容"


def test_no_result_tool_cannot_create_grounded_sources() -> None:
    provider = ScriptedProvider(
        [
            function_turn("call-1", "search_announcements", announcement_args()),
            final_turn("查不到符合條件的資料。", [], kind=ResponseKind.INSUFFICIENT),
        ]
    )

    response = ChatService(
        StubRetriever({"search_announcements": []}),
        provider,
        StubConversationStore(),
    ).answer("不存在的公告")

    assert response.answer == "查不到符合條件的資料。"
    assert response.sources == []


def test_fake_chat_routes_information_college_to_one_source_and_keeps_all_urls() -> None:
    items = [
        replace(
            evidence(f"information-{index}"),
            title=f"資訊學院公告{index}",
            url=f"https://ccs.nptu.edu.tw/a/{index}",
            unit="資訊學院",
            published_at=date(2026, 7, 14 - index),
        )
        for index in range(1, 6)
    ]
    retriever = StubRetriever({"search_announcements": items})
    refresher = RecordingRefresher(
        canonical_urls=tuple(item.url for item in items),
    )

    response = ChatService(
        retriever,
        FakeLlmProvider(),
        StubConversationStore(),
        announcement_refresher=refresher,
        unit_source_resolver=project_unit_resolver(),
    ).answer("幫我查資訊學院的最新公告")

    assert refresher.calls == ["information-college-html"]
    assert [call[0] for call in retriever.calls] == ["search_announcements"]
    assert retriever.calls[0][1][-1] == tuple(item.url for item in items)
    assert len(response.sources) == 5
    assert all(item.url in response.answer for item in items)
    assert [source.url for source in response.sources] == [item.url for item in items]


@pytest.mark.parametrize(
    ("question", "source_name", "url"),
    [
        (
            "查詢獎學金公告",
            "student-scholarship-external-html",
            "https://staf-life.nptu.edu.tw/external/1",
        ),
        (
            "查詢獎助學金公告",
            "student-scholarship-external-html",
            "https://staf-life.nptu.edu.tw/external/2",
        ),
        (
            "查詢校內獎學金公告",
            "student-scholarship-internal-html",
            "https://staf-life.nptu.edu.tw/internal/1",
        ),
    ],
)
def test_fake_chat_routes_scholarship_query_to_one_selected_source(
    question: str,
    source_name: str,
    url: str,
) -> None:
    item = replace(evidence("scholarship"), title=question, url=url, unit="生活輔導組")
    retriever = StubRetriever({"search_announcements": [item]})
    refresher = RecordingRefresher(canonical_urls=(url,))

    response = ChatService(
        retriever,
        FakeLlmProvider(),
        StubConversationStore(),
        announcement_refresher=refresher,
        unit_source_resolver=project_unit_resolver(),
    ).answer(question)

    assert refresher.calls == [source_name]
    assert retriever.calls == [
        (
            "search_announcements",
            (question, 5, AnnouncementSort.NEWEST, "生活輔導組", None, None, (url,)),
        )
    ]
    assert [source.url for source in response.sources] == [url]


@pytest.mark.parametrize(
    ("question", "message"),
    [
        ("火星學院最新公告", "無法辨識"),
        ("資訊學院研發處最新公告", "可能對應多個單位"),
        ("研發處最新公告", "尚未設定"),
    ],
)
def test_fake_chat_unit_errors_do_not_refresh_or_query(
    question: str,
    message: str,
) -> None:
    retriever = StubRetriever({})
    refresher = RecordingRefresher()

    response = ChatService(
        retriever,
        FakeLlmProvider(),
        StubConversationStore(),
        announcement_refresher=refresher,
        unit_source_resolver=project_unit_resolver(),
    ).answer(question)

    assert message in response.answer
    assert response.sources == []
    assert retriever.calls == []
    assert refresher.calls == []


def test_unit_refresh_failure_uses_only_cached_source_snapshot_and_keeps_warning() -> None:
    cached = replace(
        evidence("information-cached"),
        title="資訊學院上次成功公告",
        url="https://ccs.nptu.edu.tw/cached/1",
        unit="資訊學院",
    )
    warning = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    retriever = StubRetriever({"search_announcements": [cached]})
    refresher = RecordingRefresher(warning, (cached.url,))

    response = ChatService(
        retriever,
        FakeLlmProvider(),
        StubConversationStore(),
        announcement_refresher=refresher,
        unit_source_resolver=project_unit_resolver(),
    ).answer("資訊學院最新公告")

    assert refresher.calls == ["information-college-html"]
    assert retriever.calls[0][1][-1] == (cached.url,)
    assert response.sources[0].url == cached.url
    assert response.warning == warning
