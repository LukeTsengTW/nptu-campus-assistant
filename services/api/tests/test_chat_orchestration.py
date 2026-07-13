from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import pytest

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.rag.models import (
    ConversationContext,
    Evidence,
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)
from nptu_assistant.rag.service import ChatService, INSUFFICIENT_ANSWER
from nptu_assistant.rag.tools import AnnouncementSort


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
    ) -> list[Evidence]:
        self.calls.append(("search_announcements", (query, limit, sort, unit, date_from, date_to)))
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
