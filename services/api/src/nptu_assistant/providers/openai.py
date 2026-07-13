from __future__ import annotations

from typing import Any

from openai import APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ConfigDict, ValidationError

from nptu_assistant.api.errors import AppError
from nptu_assistant.rag.models import (
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)


class _FinalAnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    used_source_ids: list[str]
    warning: str | None
    response_kind: ResponseKind


_FINAL_ANSWER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "used_source_ids": {"type": "array", "items": {"type": "string"}},
        "warning": {"type": ["string", "null"]},
        "response_kind": {
            "type": "string",
            "enum": ["grounded", "clarification", "insufficient"],
        },
    },
    "required": ["answer", "used_source_ids", "warning", "response_kind"],
    "additionalProperties": False,
}


class OpenAIEmbeddingProvider:
    def __init__(self, client: Any, model: str, dimensions: int = 1536) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(
                model=self._model,
                input=texts,
                dimensions=self._dimensions,
                encoding_format="float",
                timeout=30.0,
            )
        except Exception as exc:
            raise AppError(
                "embedding_provider_error",
                "向量服務暫時無法使用。",
                status_code=503,
            ) from exc
        return [list(item.embedding) for item in sorted(response.data, key=lambda item: item.index)]


class OpenAILlmProvider:
    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self._model = model

    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        try:
            response = self._client.responses.create(
                model=self._model,
                instructions=instructions,
                input=input_items,
                tools=tools,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "nptu_grounded_answer",
                        "strict": True,
                        "schema": _FINAL_ANSWER_SCHEMA,
                    },
                    "verbosity": "low",
                },
                store=False,
                timeout=45.0,
            )
        except APITimeoutError as exc:
            raise AppError(
                "llm_timeout",
                "回答服務逾時，請稍後再試。",
                status_code=504,
            ) from exc
        except RateLimitError as exc:
            raise AppError(
                "llm_rate_limited",
                "回答服務目前忙碌，請稍後再試。",
                status_code=503,
            ) from exc
        except APIError as exc:
            raise AppError(
                "llm_provider_error",
                "回答服務暫時無法使用。",
                status_code=503,
            ) from exc
        except Exception as exc:
            raise AppError(
                "llm_provider_error",
                "回答服務暫時無法使用。",
                status_code=503,
            ) from exc

        output_items: list[dict[str, object]] = []
        tool_calls: list[ToolCall] = []
        for item in response.output:
            dumped = (
                dict(item)
                if isinstance(item, dict)
                else item.model_dump(exclude_none=True)
            )
            output_items.append(dumped)
            if dumped.get("type") == "function_call":
                tool_calls.append(
                    ToolCall(
                        call_id=str(dumped["call_id"]),
                        name=str(dumped["name"]),
                        arguments=str(dumped["arguments"]),
                    )
                )
        if tool_calls:
            return ModelTurn(output_items=output_items, tool_calls=tool_calls)

        try:
            payload = _FinalAnswerPayload.model_validate_json(response.output_text)
        except (ValidationError, ValueError, TypeError) as exc:
            raise AppError(
                "llm_invalid_response",
                "回答服務回傳無法處理的格式。",
                status_code=502,
            ) from exc
        return ModelTurn(
            output_items=output_items,
            generated=GeneratedAnswer(
                answer=payload.answer,
                used_source_ids=payload.used_source_ids,
                warning=payload.warning,
                response_kind=payload.response_kind,
            ),
        )
