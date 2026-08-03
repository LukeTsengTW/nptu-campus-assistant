from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.rag.completeness import (
    CompletenessAction,
    CompletenessConfig,
    DbFirstCompletenessPolicy,
    QueryIntent,
)
from nptu_assistant.rag.completeness_facts import _announcement_facts_from_rows
from nptu_assistant.rag.models import Evidence


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)
URL = "https://www.nptu.edu.tw/p4-announcement"


def _evidence() -> Evidence:
    return Evidence(
        id="p4-announcement",
        kind=AnswerType.ANNOUNCEMENT,
        title="P4 公告",
        url=URL,
        unit="P4 單位",
        published_at=date(2026, 8, 3),
        content="P4 公告有足夠的官方內容可供完整性判斷。" * 3,
        score=0.95,
    )


def _facts(
    rows: list[tuple[object, ...]],
    source_rows: list[tuple[str, str, datetime | None]],
    configured_targets: list[tuple[str, str, str]],
):
    return _announcement_facts_from_rows(
        [_evidence()],
        rows,  # type: ignore[arg-type]
        source_rows,
        configured_targets=configured_targets,
        urls=(URL,),
        unit="P4 單位",
        now=NOW,
        strong_score=0.82,
        min_content_chars=40,
        soft_stale=timedelta(minutes=90),
        hard_stale=timedelta(minutes=360),
    )


def test_announcement_page_failure_and_hash_mismatch_never_count_as_complete() -> None:
    facts = _facts(
        [
            (
                URL,
                "P4 單位",
                NOW,
                [URL],
                NOW,
                False,
                "b" * 64,
                "a" * 64,
                "success",
                "failed",
                None,
                None,
                None,
            )
        ],
        [("p4-source", "https://www.nptu.edu.tw/p4-listing", NOW)],
        [("p4-source", "https://www.nptu.edu.tw/p4-listing", "P4 單位")],
    )

    decision = DbFirstCompletenessPolicy(CompletenessConfig()).decide(
        facts=facts,
        intent=QueryIntent.ANNOUNCEMENT,
        remaining_deadline_seconds=20,
    )

    assert facts.failed_ingestion_count == 1
    assert facts.content_hash_in_sync_count == 0
    assert decision.action is CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH
    assert "relevant_ingestion_failed" in decision.reason_codes


def test_announcement_coverage_counts_missing_configured_source_as_uncovered() -> None:
    facts = _facts(
        [
            (
                URL,
                "P4 單位",
                NOW,
                [URL],
                NOW,
                False,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        ],
        [("p4-source-a", "https://www.nptu.edu.tw/p4-a", NOW)],
        [
            ("p4-source-a", "https://www.nptu.edu.tw/p4-a", "P4 單位"),
            ("p4-source-b", "https://www.nptu.edu.tw/p4-b", "P4 單位"),
        ],
    )

    decision = DbFirstCompletenessPolicy(
        CompletenessConfig(min_strong_evidence=1)
    ).decide(
        facts=facts,
        intent=QueryIntent.ANNOUNCEMENT,
        remaining_deadline_seconds=20,
    )

    assert facts.source_coverage_ratio == 0.5
    assert decision.action is CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH
    assert "insufficient_source_coverage" in decision.reason_codes


def test_announcement_detail_warning_is_retryable_not_fresh_complete() -> None:
    facts = _facts(
        [
            (
                URL,
                "P4 單位",
                NOW,
                [URL],
                NOW,
                False,
                None,
                None,
                "success",
                "success",
                "公告詳情暫時無法取得，使用列表內容",
                None,
                None,
            )
        ],
        [("p4-source", "https://www.nptu.edu.tw/p4-listing", NOW)],
        [("p4-source", "https://www.nptu.edu.tw/p4-listing", "P4 單位")],
    )

    decision = DbFirstCompletenessPolicy(
        CompletenessConfig(min_strong_evidence=1)
    ).decide(
        facts=facts,
        intent=QueryIntent.ANNOUNCEMENT,
        remaining_deadline_seconds=20,
    )

    assert facts.failed_ingestion_count == 1
    assert decision.action is CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH
    assert "relevant_ingestion_failed" in decision.reason_codes


def test_announcement_not_in_latest_source_snapshot_never_counts_as_current() -> None:
    facts = _facts(
        [
            (
                URL,
                "P4 單位",
                NOW,
                ["https://www.nptu.edu.tw/p4-newer-announcement"],
                NOW,
                False,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        ],
        [("p4-source", "https://www.nptu.edu.tw/p4-listing", NOW)],
        [("p4-source", "https://www.nptu.edu.tw/p4-listing", "P4 單位")],
    )

    decision = DbFirstCompletenessPolicy(
        CompletenessConfig(min_strong_evidence=1)
    ).decide(
        facts=facts,
        intent=QueryIntent.LATEST,
        remaining_deadline_seconds=20,
    )

    assert facts.current_document_count == 0
    assert facts.content_hash_in_sync_count == 0
    assert decision.action is CompletenessAction.USE_BOUNDED_LIVE_FALLBACK
    assert "missing_listing_coverage" in decision.reason_codes
