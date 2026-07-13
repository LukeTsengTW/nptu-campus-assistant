from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.db.models import Conversation, ConversationEvent
from nptu_assistant.rag.models import ConversationContext, Evidence


CONVERSATION_TTL = timedelta(hours=24)
MAX_CONTEXT_MESSAGES = 12
MAX_CONTEXT_CHARACTERS = 16_000
MAX_TOOL_CONTEXT_CHARACTERS = 8_000
_SENSITIVE = re.compile(
    r"(?:密碼|password|cookie|學號|成績|身分證|session\s*[:=]|token\s*[:=])|"
    r"\b[A-Z][12]\d{8}\b|\b\d{8,10}\b",
    re.IGNORECASE,
)
_REDACTED = "[已隱去可能的個人或敏感資料]"


@dataclass(frozen=True, slots=True)
class StoredConversationEvent:
    sequence: int
    event_type: str
    content: str | None
    metadata: dict[str, object]
    created_at: datetime


def redact_sensitive_text(text: str) -> str:
    return _REDACTED if _SENSITIVE.search(text) else text


def _evidence_from_metadata(value: object) -> Evidence | None:
    if not isinstance(value, dict):
        return None
    try:
        kind = AnswerType(str(value["kind"]))
        if kind is AnswerType.INSUFFICIENT_INFORMATION:
            return None
        published_raw = value.get("published_at")
        published_at = date.fromisoformat(str(published_raw)) if published_raw else None
        return Evidence(
            id=str(value["id"]),
            kind=kind,
            title=str(value["title"]),
            url=str(value["url"]),
            unit=str(value["unit"]),
            published_at=published_at,
            content="",
            score=float(value.get("score", 0.65)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def build_conversation_context(
    conversation_id: str,
    events: list[StoredConversationEvent],
) -> ConversationContext:
    ordered = sorted(events, key=lambda item: item.sequence)
    tool_events = [item for item in ordered if item.event_type == "tool"][-2:]
    evidence_by_id: dict[str, Evidence] = {}
    summaries: list[dict[str, object]] = []
    for item in tool_events:
        summaries.append(item.metadata)
        results = item.metadata.get("results", [])
        if isinstance(results, list):
            for raw in results:
                evidence = _evidence_from_metadata(raw)
                if evidence:
                    evidence_by_id[evidence.id] = evidence

    input_items: list[dict[str, object]] = []
    used_characters = 0
    if summaries:
        summary = "最近工具結果摘要：" + json.dumps(summaries, ensure_ascii=False)
        summary = summary[:MAX_TOOL_CONTEXT_CHARACTERS]
        input_items.append({"role": "developer", "content": summary})
        used_characters = len(summary)

    message_events = [
        item for item in ordered if item.event_type in {"user", "assistant"} and item.content
    ][-MAX_CONTEXT_MESSAGES:]
    selected: list[dict[str, object]] = []
    remaining = MAX_CONTEXT_CHARACTERS - used_characters
    for item in reversed(message_events):
        if remaining <= 0:
            break
        content = str(item.content)
        if len(content) > remaining:
            content = content[:remaining]
        selected.append({"role": item.event_type, "content": content})
        remaining -= len(content)
    input_items.extend(reversed(selected))
    return ConversationContext(
        conversation_id=conversation_id,
        input_items=input_items,
        evidence=list(evidence_by_id.values()),
    )


def _evidence_metadata(item: Evidence) -> dict[str, object]:
    return {
        "id": item.id,
        "kind": item.kind.value,
        "title": item.title,
        "url": item.url,
        "unit": item.unit,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "score": item.score,
    }


class SqlConversationStore:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def load_or_create(self, conversation_id: str | None) -> ConversationContext:
        now = datetime.now(timezone.utc)
        with self._factory.begin() as session:
            session.execute(delete(Conversation).where(Conversation.expires_at <= now))
            conversation = None
            if conversation_id:
                try:
                    parsed_id = uuid.UUID(conversation_id)
                except ValueError:
                    parsed_id = None
                if parsed_id:
                    conversation = session.scalar(
                        select(Conversation).where(
                            Conversation.id == parsed_id,
                            Conversation.expires_at > now,
                        )
                    )
            if conversation is None:
                conversation = Conversation(expires_at=now + CONVERSATION_TTL)
                session.add(conversation)
                session.flush()
            else:
                conversation.expires_at = now + CONVERSATION_TTL
            rows = session.scalars(
                select(ConversationEvent)
                .where(ConversationEvent.conversation_id == conversation.id)
                .order_by(ConversationEvent.sequence)
            ).all()
            events = [
                StoredConversationEvent(
                    sequence=row.sequence,
                    event_type=row.event_type,
                    content=row.content,
                    metadata=row.event_metadata,
                    created_at=row.created_at,
                )
                for row in rows
            ]
            identifier = str(conversation.id)
        return build_conversation_context(identifier, events)

    def save_turn(
        self,
        *,
        conversation_id: str,
        user_message: str,
        assistant_message: str,
        warning: str | None,
        tool_events: list[dict[str, object]],
        sources: list[Evidence],
    ) -> None:
        parsed_id = uuid.UUID(conversation_id)
        now = datetime.now(timezone.utc)
        with self._factory.begin() as session:
            conversation = session.scalar(
                select(Conversation)
                .where(Conversation.id == parsed_id)
                .with_for_update()
            )
            if conversation is None:
                raise ValueError("對話不存在或已過期")
            sequence = session.scalar(
                select(func.max(ConversationEvent.sequence)).where(
                    ConversationEvent.conversation_id == parsed_id
                )
            ) or 0

            def add_event(
                event_type: str,
                content: str | None,
                metadata: dict[str, object] | None = None,
            ) -> None:
                nonlocal sequence
                sequence += 1
                session.add(
                    ConversationEvent(
                        conversation_id=parsed_id,
                        sequence=sequence,
                        event_type=event_type,
                        content=content,
                        event_metadata=metadata or {},
                    )
                )

            add_event("user", redact_sensitive_text(user_message))
            for tool_event in tool_events:
                raw_evidence = tool_event.get("evidence", [])
                evidence = raw_evidence if isinstance(raw_evidence, list) else []
                add_event(
                    "tool",
                    None,
                    {
                        "tool_name": str(tool_event.get("tool_name", "unknown")),
                        "results": [
                            _evidence_metadata(item)
                            for item in evidence[:20]
                            if isinstance(item, Evidence)
                        ],
                    },
                )
            add_event(
                "assistant",
                redact_sensitive_text(assistant_message),
                {
                    "warning": redact_sensitive_text(warning) if warning else None,
                    "sources": [_evidence_metadata(item) for item in sources],
                },
            )
            conversation.expires_at = now + CONVERSATION_TTL

    def delete(self, conversation_id: str) -> bool:
        try:
            parsed_id = uuid.UUID(conversation_id)
        except ValueError:
            return False
        with self._factory.begin() as session:
            result = session.execute(delete(Conversation).where(Conversation.id == parsed_id))
            return bool(result.rowcount)
