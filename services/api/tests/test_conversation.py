from __future__ import annotations

from datetime import datetime, timezone

from nptu_assistant.db.models import Conversation, ConversationEvent
from nptu_assistant.rag.conversation import (
    StoredConversationEvent,
    build_conversation_context,
    redact_sensitive_text,
)


def test_conversation_tables_have_expiry_sequence_and_cascade_contract() -> None:
    assert {"id", "created_at", "updated_at", "expires_at"} <= set(Conversation.__table__.columns.keys())
    assert {"conversation_id", "sequence", "event_type", "content", "metadata"} <= set(
        ConversationEvent.__table__.columns.keys()
    )
    conversation_fk = next(iter(ConversationEvent.__table__.c.conversation_id.foreign_keys))
    assert conversation_fk.ondelete == "CASCADE"
    assert any(
        set(constraint.columns.keys()) == {"conversation_id", "sequence"}
        for constraint in ConversationEvent.__table__.constraints
    )


def test_sensitive_values_are_not_retained_in_conversation_text() -> None:
    inputs = [
        "我的密碼是 secret-123",
        "Cookie: session=abc",
        "學號 123456789",
        "身分證 A123456789",
        "我的成績是 95 分",
    ]

    for value in inputs:
        redacted = redact_sensitive_text(value)
        assert redacted == "[已隱去可能的個人或敏感資料]"
        assert value not in redacted
    assert redact_sensitive_text("最近五則公告") == "最近五則公告"


def event(sequence: int, event_type: str, content: str | None, metadata: dict | None = None) -> StoredConversationEvent:
    return StoredConversationEvent(
        sequence=sequence,
        event_type=event_type,
        content=content,
        metadata=metadata or {},
        created_at=datetime.now(timezone.utc),
    )


def test_context_keeps_at_most_twelve_messages_and_sixteen_thousand_characters() -> None:
    events = [event(index, "user", f"訊息{index}-" + ("字" * 2_000)) for index in range(1, 16)]

    context = build_conversation_context("conversation-1", events)
    messages = [item for item in context.input_items if item.get("role") in {"user", "assistant"}]

    assert len(messages) <= 12
    assert sum(len(str(item["content"])) for item in context.input_items) <= 16_000
    assert messages[-1]["content"].startswith("訊息15-")


def test_context_keeps_only_two_latest_tool_result_sets_and_rebuilds_evidence() -> None:
    def tool_metadata(source_id: str) -> dict[str, object]:
        return {
            "tool_name": "search_announcements",
            "results": [
                {
                    "id": source_id,
                    "kind": "announcement",
                    "title": f"公告 {source_id}",
                    "url": f"https://www.nptu.edu.tw/{source_id}",
                    "unit": "教務處",
                    "published_at": "2026-07-12",
                    "score": 0.8,
                }
            ],
        }

    events = [
        event(1, "tool", None, tool_metadata("old")),
        event(2, "tool", None, tool_metadata("middle")),
        event(3, "tool", None, tool_metadata("latest")),
        event(4, "assistant", "上一輪回答"),
    ]

    context = build_conversation_context("conversation-1", events)
    developer_context = str(context.input_items[0]["content"])

    assert "latest" in developer_context
    assert "middle" in developer_context
    assert "old" not in developer_context
    assert [item.id for item in context.evidence] == ["middle", "latest"]
