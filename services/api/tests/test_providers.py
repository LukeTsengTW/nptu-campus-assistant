from __future__ import annotations

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
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
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
                "output": '{"results":[{"id":"announcement-1","title":"測試公告","content":"公告內容","score":0.8}],"count":1}',
            },
            {
                "type": "function_call_output",
                "call_id": "fake-documents",
                "output": '{"results":[{"id":"document-1","title":"無關文件","content":"無關內容","score":0.2}],"count":1}',
            },
        ],
        tools=[],
    )

    assert {call.name for call in first.tool_calls} == {
        "search_announcements",
        "search_documents",
    }
    assert second.generated is not None
    assert second.generated.used_source_ids == ["announcement-1"]
    assert "公告內容" in second.generated.answer


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
    assert responses.captured["tools"] == [{"type": "function", "name": "search_documents"}]


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
    assert set(output_format["schema"]["required"]) == set(output_format["schema"]["properties"])


def test_openai_provider_maps_invalid_output_timeout_and_rate_limit() -> None:
    invalid = OpenAILlmProvider(
        client_with(Responses(SimpleNamespace(output=[], output_text="not-json"))),
        "gpt-5.4-mini",
    )
    timeout = OpenAILlmProvider(
        client_with(Responses(error=APITimeoutError(request=httpx.Request("POST", "https://api.openai.com")))),
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
