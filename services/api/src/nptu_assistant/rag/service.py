from __future__ import annotations

import re
from typing import Protocol

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.schemas import (
    AnswerType,
    ChatResponse,
    Confidence,
    SourceReference,
)
from nptu_assistant.rag.models import (
    ConversationContext,
    Evidence,
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
)
from nptu_assistant.rag.prompts import SYSTEM_INSTRUCTIONS
from nptu_assistant.rag.tools import (
    AnnouncementRefresher,
    StructuredRetriever,
    ToolExecutor,
    tool_definitions,
)


INSUFFICIENT_ANSWER = "目前收錄的官方資料不足以確認。"
MAX_TOOL_ROUNDS = 4
_INTERNAL_SOURCE_ID = re.compile(
    r"[\[（(]?\s*\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b\s*[\]）)]?"
)
_URL = re.compile(r"https?://[^\s<>\"'，。；：、！？）)\]}]+")
_URL_TRAILING = "，。；：、！？,.!?;:）)]}"


class LlmProvider(Protocol):
    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn: ...


class ConversationStore(Protocol):
    def load_or_create(self, conversation_id: str | None) -> ConversationContext: ...

    def save_turn(self, **kwargs: object) -> None: ...

    def delete(self, conversation_id: str) -> bool: ...


def confidence_for_score(score: float) -> Confidence:
    if score >= 0.75:
        return Confidence.HIGH
    if score >= 0.55:
        return Confidence.MEDIUM
    return Confidence.LOW


def sanitize_user_facing_text(text: str, *, allowed_urls: set[str] | None = None) -> str:
    allowed_urls = allowed_urls or set()
    cleaned = _INTERNAL_SOURCE_ID.sub("", text)

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        candidate = raw.rstrip(_URL_TRAILING)
        trailing = raw[len(candidate) :]
        return raw if candidate in allowed_urls else trailing

    cleaned = _URL.sub(replace_url, cleaned)
    cleaned = re.sub(r"[ \t]+([，。；：、])", r"\1", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()


class ChatService:
    def __init__(
        self,
        retriever: StructuredRetriever,
        llm: LlmProvider,
        conversation_store: ConversationStore,
        announcement_refresher: AnnouncementRefresher | None = None,
    ) -> None:
        self._llm = llm
        self._conversation_store = conversation_store
        self._tool_executor = ToolExecutor(retriever, announcement_refresher)

    def delete_conversation(self, conversation_id: str) -> bool:
        return self._conversation_store.delete(conversation_id)

    def answer(self, question: str, conversation_id: str | None = None) -> ChatResponse:
        context = self._conversation_store.load_or_create(conversation_id)
        input_items = [
            *context.input_items,
            {"role": "user", "content": question},
        ]
        evidence_by_id = {item.id: item for item in context.evidence}
        tool_events: list[dict[str, object]] = []
        tool_warnings: list[str] = []
        tool_rounds = 0

        while True:
            turn = self._llm.create_turn(
                instructions=SYSTEM_INSTRUCTIONS,
                input_items=input_items,
                tools=tool_definitions(),
            )
            input_items.extend(turn.output_items)
            calls = turn.tool_calls or []
            if calls:
                if tool_rounds >= MAX_TOOL_ROUNDS:
                    raise AppError(
                        "tool_round_limit",
                        "資料查詢次數超過安全上限，請縮小問題範圍後重試。",
                        status_code=503,
                    )
                tool_rounds += 1
                for call in calls:
                    result = self._tool_executor.execute(call.name, call.arguments)
                    for item in result.evidence:
                        evidence_by_id[item.id] = item
                    if result.warning and result.warning not in tool_warnings:
                        tool_warnings.append(result.warning)
                    tool_events.append(
                        {
                            "tool_name": call.name,
                            "output": result.output,
                            "evidence": result.evidence,
                        }
                    )
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call.call_id,
                            "output": result.output,
                        }
                    )
                continue

            if turn.generated is None:
                raise AppError(
                    "llm_invalid_response",
                    "回答服務回傳無法處理的格式。",
                    status_code=502,
                )
            response = self._build_response(
                context.conversation_id,
                turn.generated,
                evidence_by_id,
                tool_warnings,
            )
            self._conversation_store.save_turn(
                conversation_id=context.conversation_id,
                user_message=question,
                assistant_message=response.answer,
                warning=response.warning,
                tool_events=tool_events,
                sources=[evidence_by_id[source.id] for source in response.sources],
            )
            return response

    def _build_response(
        self,
        conversation_id: str,
        generated: GeneratedAnswer,
        evidence_by_id: dict[str, Evidence],
        tool_warnings: list[str],
    ) -> ChatResponse:
        used: list[Evidence] = []
        seen: set[str] = set()
        for source_id in generated.used_source_ids:
            if source_id in seen or source_id not in evidence_by_id:
                continue
            seen.add(source_id)
            used.append(evidence_by_id[source_id])

        allowed_urls = {item.url for item in used}
        answer = sanitize_user_facing_text(generated.answer, allowed_urls=allowed_urls)
        warning = (
            sanitize_user_facing_text(generated.warning, allowed_urls=allowed_urls)
            if generated.warning
            else None
        )
        warning_parts = [item for item in [warning, *tool_warnings] if item]
        warning = "\n".join(dict.fromkeys(warning_parts)) or None
        if not answer:
            answer = INSUFFICIENT_ANSWER

        if not used:
            if generated.response_kind not in {
                ResponseKind.CLARIFICATION,
                ResponseKind.INSUFFICIENT,
            }:
                answer = INSUFFICIENT_ANSWER
                warning = INSUFFICIENT_ANSWER
            return ChatResponse(
                conversation_id=conversation_id,
                answer=answer,
                answer_type=AnswerType.INSUFFICIENT_INFORMATION,
                confidence=Confidence.LOW,
                sources=[],
                warning=warning,
            )

        return ChatResponse(
            conversation_id=conversation_id,
            answer=answer,
            answer_type=used[0].kind,
            confidence=confidence_for_score(used[0].score),
            sources=[
                SourceReference(
                    id=item.id,
                    kind=item.kind,
                    title=item.title,
                    url=item.url,
                    unit=item.unit,
                    published_at=item.published_at,
                )
                for item in used
            ],
            warning=warning,
        )
