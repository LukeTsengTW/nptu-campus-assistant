from __future__ import annotations

from types import SimpleNamespace

from nptu_assistant.providers.fake import FakeEmbeddingProvider, FakeLlmProvider
from nptu_assistant.providers.openai import OpenAILlmProvider
from nptu_assistant.rag.models import Evidence
from nptu_assistant.api.schemas import AnswerType


def test_fake_embedding_is_deterministic_and_has_requested_dimensions() -> None:
    provider = FakeEmbeddingProvider(dimensions=8)

    assert provider.embed(["測試"])[0] == provider.embed(["測試"])[0]
    assert len(provider.embed(["測試"])[0]) == 8


def test_openai_responses_provider_uses_current_structured_output_shape() -> None:
    captured: dict[str, object] = {}

    class Responses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                output_text='{"answer":"ok","used_source_ids":["source-1"],"warning":null}'
            )

    provider = object.__new__(OpenAILlmProvider)
    provider._client = SimpleNamespace(responses=Responses())
    provider._model = "gpt-5.4-mini"
    evidence = Evidence(
        id="source-1",
        kind=AnswerType.ANNOUNCEMENT,
        title="測試公告",
        url="https://www.nptu.edu.tw/a",
        unit="測試單位",
        published_at=None,
        content="測試內容",
        score=0.8,
    )

    provider.generate("最近公告", [evidence])

    output_format = captured["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["name"] == "nptu_grounded_answer"
    assert output_format["strict"] is True
    assert "schema" in output_format
    assert "json_schema" not in output_format
    assert captured["store"] is False
    assert "不得在 answer" in captured["instructions"]


def test_fake_llm_uses_known_source_ids_only() -> None:
    evidence = Evidence(
        id="source-1",
        kind=AnswerType.ANNOUNCEMENT,
        title="測試公告",
        url="https://www.nptu.edu.tw/a",
        unit="教務處",
        published_at=None,
        content="公告內容",
        score=0.8,
    )

    generated = FakeLlmProvider().generate("最近公告", [evidence])

    assert generated.used_source_ids == ["source-1"]
    assert "公告內容" in generated.answer
