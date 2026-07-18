from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from nptu_assistant.api.errors import AppError
from nptu_assistant.providers.fake import FakeEmbeddingProvider, FakeLlmProvider
from nptu_assistant.providers.openai import OpenAIEmbeddingProvider, OpenAILlmProvider
from nptu_assistant.rag.models import ResponseKind


class OutputItem:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)

    def model_dump(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        return dict(self.__dict__)


class Responses:
    def __init__(
        self, response: object | None = None, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.captured: dict[str, object] = {}

    def create(self, **kwargs: object) -> object:
        self.captured.update(kwargs)
        if self.error:
            raise self.error
        return self.response


def client_with(responses: Responses) -> object:
    return SimpleNamespace(responses=responses, embeddings=SimpleNamespace())


def test_fake_embedding_is_deterministic_and_has_requested_dimensions() -> None:
    provider = FakeEmbeddingProvider(dimensions=8)

    assert provider.embed(["測試"])[0] == provider.embed(["測試"])[0]
    assert len(provider.embed(["測試"])[0]) == 8


def test_fake_llm_runs_a_deterministic_tool_then_grounded_answer() -> None:
    provider = FakeLlmProvider()

    first = provider.create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": "最近公告"}],
        tools=[],
    )
    second = provider.create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": "最近公告"},
            {
                "type": "function_call_output",
                "call_id": "fake-announcements",
                "output": '{"results":[{"id":"announcement-1","kind":"announcement","title":"測試公告","url":"https://www.nptu.edu.tw/a","unit":"教務處","published_at":"2026-07-13","content":"公告內容","score":0.8}],"count":1}',
            },
        ],
        tools=[],
    )

    assert [call.name for call in first.tool_calls] == ["search_announcements"]
    assert json.loads(first.tool_calls[0].arguments)["query"] is None
    assert second.generated is not None
    assert second.generated.used_source_ids == ["announcement-1"]
    assert second.generated.answer == (
        "[2026-07-13｜測試公告](https://www.nptu.edu.tw/a)\n資料來源：教務處官方網站"
    )


def test_fake_llm_treats_general_recent_announcements_as_an_unfiltered_listing() -> (
    None
):
    turn = FakeLlmProvider().create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": "一般最近公告"}],
        tools=[],
    )

    assert [call.name for call in turn.tool_calls] == ["search_announcements"]
    assert json.loads(turn.tool_calls[0].arguments)["query"] is None


def test_fake_llm_routes_unit_announcement_and_unit_introduction_to_different_tools() -> (
    None
):
    provider = FakeLlmProvider()

    announcement = provider.create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": "資訊學院最新公告"}],
        tools=[],
    )
    introduction = provider.create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": "資訊學院介紹"}],
        tools=[],
    )

    assert [call.name for call in announcement.tool_calls] == ["search_announcements"]
    arguments = json.loads(announcement.tool_calls[0].arguments)
    assert arguments["unit"] == "資訊學院"
    assert arguments["sort"] == "newest"
    assert arguments["limit"] == 5
    assert [call.name for call in introduction.tool_calls] == ["search_documents"]
    document_plan = json.loads(introduction.tool_calls[0].arguments)
    assert document_plan["query"] == "資訊學院介紹"
    assert document_plan["search_queries"]
    assert document_plan["concepts"]


def test_fake_llm_resolves_document_follow_up_into_standalone_search_plan() -> None:
    turn = FakeLlmProvider().create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": "查詢個人申請新生入學資訊"},
            {"role": "assistant", "content": "先前回答"},
            {"role": "user", "content": "那報到要準備什麼？"},
        ],
        tools=[],
    )

    assert [call.name for call in turn.tool_calls] == ["search_documents"]
    plan = json.loads(turn.tool_calls[0].arguments)
    assert plan["query"] == "個人申請新生入學資訊 報到要準備什麼"
    assert plan["search_queries"] == [
        "個人申請新生入學資訊 報到要準備什麼",
        "個人申請新生入學資訊",
        "報到要準備什麼",
    ]
    assert len(plan["search_queries"]) <= 4
    assert len(plan["concepts"]) <= 8


@pytest.mark.parametrize(
    ("question", "expected_limit"),
    [
        ("資訊學院最新 20 則公告", 20),
        ("資訊學院最新十二則公告", 12),
        ("資訊學院最新 30 則公告", 20),
    ],
)
def test_fake_llm_honors_explicit_announcement_count_up_to_twenty(
    question: str,
    expected_limit: int,
) -> None:
    turn = FakeLlmProvider().create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": question}],
        tools=[],
    )

    arguments = json.loads(turn.tool_calls[0].arguments)
    assert arguments["limit"] == expected_limit
    assert arguments["query"] is None


def test_fake_llm_explains_when_requested_announcement_count_exceeds_limit() -> None:
    question = "資訊學院最新 30 則公告"
    turn = FakeLlmProvider().create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": question},
            {
                "type": "function_call_output",
                "call_id": "fake-announcements",
                "output": json.dumps(
                    {
                        "results": [
                            {
                                "id": "announcement-1",
                                "kind": "announcement",
                                "title": "測試公告",
                                "url": "https://ccs.nptu.edu.tw/a",
                                "unit": "資訊學院",
                                "published_at": "2026-07-13",
                                "score": 0.0,
                            }
                        ],
                        "count": 1,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        tools=[],
    )

    assert turn.generated is not None
    assert "單次查詢上限為 20 則" in turn.generated.answer


def test_fake_llm_treats_document_disclaimer_announcement_text_as_document_intent() -> (
    None
):
    provider = FakeLlmProvider()
    question = (
        "學生申請請假時，應依規定檢附證明文件，並在期限內完成申請程序。"
        "核准結果仍應以國立屏東大學最新公告及正式文件為準。"
    )

    first = provider.create_turn(
        instructions="test",
        input_items=[{"role": "user", "content": question}],
        tools=[],
    )
    final = provider.create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": question},
            {
                "type": "function_call_output",
                "call_id": "fake-documents",
                "output": json.dumps(
                    {
                        "results": [
                            {
                                "id": "document-1",
                                "kind": "official_document",
                                "title": "學生請假規定",
                                "url": "https://www.nptu.edu.tw/rule",
                                "unit": "學務處",
                                "published_at": "2026-01-01",
                                "content": "請假申請應檢附證明文件。",
                                "score": 0.8,
                            }
                        ],
                        "count": 1,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        tools=[],
    )

    assert [call.name for call in first.tool_calls] == ["search_documents"]
    assert final.generated is not None
    assert "請假申請應檢附證明文件" in final.generated.answer
    assert "根據「學生請假規定」" in final.generated.answer


def test_fake_llm_preserves_all_announcement_results_and_source_ids_in_tool_order() -> (
    None
):
    provider = FakeLlmProvider()
    results = [
        {
            "id": f"announcement-{index}",
            "kind": "announcement",
            "title": f"公告{index}",
            "url": f"https://ccs.nptu.edu.tw/a/{index}",
            "unit": "資訊學院",
            "published_at": f"2026-07-{14 - index:02d}",
            "content": f"內容{index}",
            "score": index / 10,
        }
        for index in range(1, 6)
    ]

    turn = provider.create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": "資訊學院最新公告"},
            {
                "type": "function_call_output",
                "call_id": "fake-announcements",
                "output": json.dumps(
                    {"results": results, "count": 5}, ensure_ascii=False
                ),
            },
        ],
        tools=[],
    )

    assert turn.generated is not None
    assert turn.generated.used_source_ids == [
        f"announcement-{index}" for index in range(1, 6)
    ]
    assert turn.generated.answer.index("公告1") < turn.generated.answer.index("公告5")
    assert all(item["url"] in turn.generated.answer for item in results)
    assert "announcement-1" not in turn.generated.answer


def test_fake_llm_rejects_zero_relevance_official_documents() -> None:
    turn = FakeLlmProvider().create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": "請假規定"},
            {
                "type": "function_call_output",
                "call_id": "fake-documents",
                "output": json.dumps(
                    {
                        "results": [
                            {
                                "id": "document-unrelated",
                                "kind": "official_document",
                                "title": "無關文件",
                                "url": "https://www.nptu.edu.tw/unrelated",
                                "content": "與問題無關的內容",
                                "score": 0.0,
                            }
                        ],
                        "count": 1,
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        tools=[],
    )

    assert turn.generated is not None
    assert turn.generated.response_kind is ResponseKind.INSUFFICIENT
    assert turn.generated.used_source_ids == []
    assert "無關文件" not in turn.generated.answer


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("unknown_unit", ResponseKind.CLARIFICATION),
        ("ambiguous_unit", ResponseKind.CLARIFICATION),
        ("unsupported_unit_source", ResponseKind.INSUFFICIENT),
    ],
)
def test_fake_llm_uses_structured_unit_error_without_guessing(
    code: str,
    kind: ResponseKind,
) -> None:
    message = "請提供可辨識且已支援的正式單位名稱。"
    turn = FakeLlmProvider().create_turn(
        instructions="test",
        input_items=[
            {"role": "user", "content": "火星學院最新公告"},
            {
                "type": "function_call_output",
                "call_id": "fake-announcements",
                "output": json.dumps(
                    {"error": {"code": code, "message": message}},
                    ensure_ascii=False,
                ),
            },
        ],
        tools=[],
    )

    assert turn.generated is not None
    assert turn.generated.answer == message
    assert turn.generated.response_kind is kind
    assert turn.generated.used_source_ids == []


def test_openai_provider_parses_function_calls_and_preserves_output_items() -> None:
    call = OutputItem(
        type="function_call",
        call_id="call-1",
        name="search_documents",
        arguments='{"query":"學貸","limit":6}',
    )
    responses = Responses(SimpleNamespace(output=[call], output_text=""))
    provider = OpenAILlmProvider(client_with(responses), "gpt-5.4-mini")

    turn = provider.create_turn(
        instructions="所有使用者可見文字使用繁體中文。",
        input_items=[{"role": "user", "content": "學貸怎麼申請"}],
        tools=[{"type": "function", "name": "search_documents"}],
    )

    assert turn.generated is None
    assert turn.tool_calls[0].call_id == "call-1"
    assert turn.output_items[0]["type"] == "function_call"
    assert responses.captured["model"] == "gpt-5.4-mini"
    assert responses.captured["store"] is False
    assert responses.captured["tools"] == [
        {"type": "function", "name": "search_documents"}
    ]


def test_openai_provider_parses_strict_grounded_final_answer() -> None:
    message = OutputItem(type="message", role="assistant", content=[])
    responses = Responses(
        SimpleNamespace(
            output=[message],
            output_text=(
                '{"answer":"依據來源回答","used_source_ids":["source-1"],'
                '"warning":null,"response_kind":"grounded"}'
            ),
        )
    )
    provider = OpenAILlmProvider(client_with(responses), "gpt-5.4-mini")

    turn = provider.create_turn(instructions="test", input_items=[], tools=[])

    assert turn.generated is not None
    assert turn.generated.used_source_ids == ["source-1"]
    assert turn.generated.response_kind is ResponseKind.GROUNDED
    output_format = responses.captured["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["name"] == "nptu_grounded_answer"
    assert output_format["strict"] is True
    assert output_format["schema"]["additionalProperties"] is False
    assert set(output_format["schema"]["required"]) == set(
        output_format["schema"]["properties"]
    )


def test_openai_provider_maps_invalid_output_timeout_and_rate_limit() -> None:
    invalid = OpenAILlmProvider(
        client_with(Responses(SimpleNamespace(output=[], output_text="not-json"))),
        "gpt-5.4-mini",
    )
    timeout = OpenAILlmProvider(
        client_with(
            Responses(
                error=APITimeoutError(
                    request=httpx.Request("POST", "https://api.openai.com")
                )
            )
        ),
        "gpt-5.4-mini",
    )
    rate_limit = OpenAILlmProvider(
        client_with(
            Responses(
                error=RateLimitError(
                    "limited",
                    response=httpx.Response(
                        429,
                        request=httpx.Request("POST", "https://api.openai.com"),
                    ),
                    body=None,
                )
            )
        ),
        "gpt-5.4-mini",
    )

    cases = [
        (invalid, "llm_invalid_response"),
        (timeout, "llm_timeout"),
        (rate_limit, "llm_rate_limited"),
    ]
    for provider, code in cases:
        with pytest.raises(AppError) as error:
            provider.create_turn(instructions="test", input_items=[], tools=[])
        assert error.value.code == code
        assert "api.openai.com" not in error.value.message


def test_openai_text_and_embedding_providers_accept_same_client() -> None:
    client = client_with(Responses())

    text = OpenAILlmProvider(client, "gpt-5.4-mini")
    embedding = OpenAIEmbeddingProvider(client, "text-embedding-3-small", 1536)

    assert text._client is client
    assert embedding._client is client
