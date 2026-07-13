from __future__ import annotations

import hashlib
import json

from nptu_assistant.rag.models import (
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append(
                [((digest[index % len(digest)] / 255.0) * 2.0) - 1.0 for index in range(self.dimensions)]
            )
        return vectors


class FakeLlmProvider:
    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        del instructions, tools
        outputs = [item for item in input_items if item.get("type") == "function_call_output"]
        if outputs:
            results: list[dict[str, object]] = []
            for item in outputs:
                try:
                    payload = json.loads(str(item.get("output", "{}")))
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("results"), list):
                    results.extend(value for value in payload["results"] if isinstance(value, dict))
            results = [
                item for item in results if float(item.get("score", 0.0)) >= 0.35
            ]
            results.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
            if not results:
                generated = GeneratedAnswer(
                    answer="目前查不到符合條件的資料。",
                    used_source_ids=[],
                    response_kind=ResponseKind.INSUFFICIENT,
                )
            else:
                first = results[0]
                generated = GeneratedAnswer(
                    answer=f"根據「{first.get('title', '來源')}」：{first.get('content', '')}",
                    used_source_ids=[str(first["id"])],
                    response_kind=ResponseKind.GROUNDED,
                )
            return ModelTurn(
                output_items=[{"type": "message", "role": "assistant"}],
                generated=generated,
            )

        question = next(
            (
                str(item.get("content", ""))
                for item in reversed(input_items)
                if item.get("role") == "user"
            ),
            "",
        )
        announcement_arguments = json.dumps(
            {
                "query": question or None,
                "limit": 5,
                "sort": "relevance",
                "unit": None,
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        )
        document_arguments = json.dumps(
            {"query": question, "limit": 6},
            ensure_ascii=False,
        )
        calls = [
            ToolCall("fake-announcements", "search_announcements", announcement_arguments),
            ToolCall("fake-documents", "search_documents", document_arguments),
        ]
        return ModelTurn(
            output_items=[
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in calls
            ],
            tool_calls=calls,
        )
