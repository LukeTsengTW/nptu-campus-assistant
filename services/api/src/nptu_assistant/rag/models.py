from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from nptu_assistant.api.schemas import AnswerType


@dataclass(frozen=True, slots=True)
class Evidence:
    id: str
    kind: AnswerType
    title: str
    url: str
    unit: str
    published_at: date | None
    content: str
    score: float


class ResponseKind(StrEnum):
    GROUNDED = "grounded"
    CLARIFICATION = "clarification"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    answer: str
    used_source_ids: list[str]
    warning: str | None = None
    response_kind: ResponseKind = ResponseKind.GROUNDED


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ModelTurn:
    output_items: list[dict[str, object]]
    tool_calls: list[ToolCall] | None = None
    generated: GeneratedAnswer | None = None


@dataclass(frozen=True, slots=True)
class ConversationContext:
    conversation_id: str
    input_items: list[dict[str, object]]
    evidence: list[Evidence]
