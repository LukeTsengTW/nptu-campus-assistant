from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    answer: str
    used_source_ids: list[str]
    warning: str | None = None
