from __future__ import annotations

import re
from typing import Protocol

from nptu_assistant.api.schemas import (
    AnswerType,
    ChatResponse,
    Confidence,
    SourceReference,
)
from nptu_assistant.rag.models import Evidence, GeneratedAnswer
from nptu_assistant.rag.routing import QuestionRoute, route_question


INSUFFICIENT_ANSWER = "目前收錄的官方資料不足以確認。"
_INTERNAL_SOURCE_ID = re.compile(
    r"[\[（(]?\s*\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b\s*[\]）)]?"
)


class Retriever(Protocol):
    def search(self, question: str, route: QuestionRoute) -> list[Evidence]: ...


class LlmProvider(Protocol):
    def generate(self, question: str, evidence: list[Evidence]) -> GeneratedAnswer: ...


def confidence_for_score(score: float) -> Confidence:
    if score >= 0.75:
        return Confidence.HIGH
    if score >= 0.55:
        return Confidence.MEDIUM
    return Confidence.LOW


def insufficient_response() -> ChatResponse:
    return ChatResponse(
        answer=INSUFFICIENT_ANSWER,
        answer_type=AnswerType.INSUFFICIENT_INFORMATION,
        confidence=Confidence.LOW,
        sources=[],
        warning=INSUFFICIENT_ANSWER,
    )


def sanitize_user_facing_text(text: str) -> str:
    cleaned = _INTERNAL_SOURCE_ID.sub("", text)
    cleaned = re.sub(r"[ \t]+([，。；：、])", r"\1", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()


class ChatService:
    def __init__(self, retriever: Retriever, llm: LlmProvider) -> None:
        self._retriever = retriever
        self._llm = llm

    def answer(self, question: str) -> ChatResponse:
        evidence = self._retriever.search(question, route_question(question))
        evidence = sorted(evidence, key=lambda item: item.score, reverse=True)[:6]
        if not evidence or evidence[0].score < 0.35:
            return insufficient_response()
        generated = self._llm.generate(question, evidence)
        by_id = {item.id: item for item in evidence}
        used = [by_id[source_id] for source_id in generated.used_source_ids if source_id in by_id]
        if not used:
            return insufficient_response()
        answer = sanitize_user_facing_text(generated.answer)
        if not answer:
            return insufficient_response()
        answer_type = used[0].kind
        return ChatResponse(
            answer=answer,
            answer_type=answer_type,
            confidence=confidence_for_score(used[0].score),
            sources=[
                SourceReference(
                    title=item.title,
                    url=item.url,
                    unit=item.unit,
                    published_at=item.published_at,
                )
                for item in used
            ],
            warning=(sanitize_user_facing_text(generated.warning) or None)
            if generated.warning
            else None,
        )
