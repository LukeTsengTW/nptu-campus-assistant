from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from nptu_assistant.crawlers.config import DbFirstCompletenessConfig

from nptu_assistant.rag.completeness import (
    CompletenessAction,
    CompletenessConfig,
    CompletenessFacts,
    DbFirstCompletenessPolicy,
    QueryIntent,
)


def _facts(**changes: object) -> CompletenessFacts:
    defaults = CompletenessFacts(
        evidence_count=2,
        unique_url_count=2,
        strong_evidence_count=2,
        top_score=0.91,
        score_margin=0.18,
        current_document_count=2,
        fresh_count=2,
        exact_scope_match_count=0,
        content_hash_in_sync_count=2,
        source_coverage_ratio=1.0,
    )
    return replace(defaults, **changes)


@pytest.mark.parametrize(
    ("facts", "intent", "expected", "reason"),
    [
        (
            _facts(),
            QueryIntent.TOPIC,
            CompletenessAction.USE_DB,
            "fresh_strong_evidence",
        ),
        (
            _facts(strong_evidence_count=1, exact_scope_match_count=1),
            QueryIntent.SCOPED_TOPIC,
            CompletenessAction.USE_DB,
            "exact_scoped_match",
        ),
        (
            _facts(strong_evidence_count=1, exact_scope_match_count=1),
            QueryIntent.ANNOUNCEMENT,
            CompletenessAction.USE_DB,
            "exact_scoped_match",
        ),
        (
            _facts(exact_scope_match_count=0),
            QueryIntent.SCOPED_TOPIC,
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            "missing_unit_coverage",
        ),
        (
            _facts(homepage_only_count=1, evidence_count=1, unique_url_count=1),
            QueryIntent.TOPIC,
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            "homepage_only_for_topic_query",
        ),
        (
            _facts(current_document_count=0, strong_evidence_count=0),
            QueryIntent.TOPIC,
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            "site_map_only_without_document",
        ),
        (
            _facts(fresh_count=0, soft_stale_count=2),
            QueryIntent.TOPIC,
            CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
            "stale_but_usable",
        ),
        (
            _facts(fresh_count=0, hard_stale_count=2),
            QueryIntent.LATEST,
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            "recent_query_requires_freshness",
        ),
        (
            _facts(pending_ingestion_count=1),
            QueryIntent.TOPIC,
            CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
            "relevant_ingestion_pending",
        ),
        (
            _facts(failed_ingestion_count=1),
            QueryIntent.TOPIC,
            CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
            "relevant_ingestion_failed",
        ),
        (
            _facts(content_hash_in_sync_count=0),
            QueryIntent.TOPIC,
            CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
            "relevant_ingestion_pending",
        ),
        (
            _facts(active_refresh_count=1, strong_evidence_count=0),
            QueryIntent.TOPIC,
            CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING,
            "active_refresh_in_progress",
        ),
        (
            _facts(incomplete_announcement_count=1),
            QueryIntent.ANNOUNCEMENT,
            CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING,
            "terminal_incomplete",
        ),
        (
            _facts(strong_evidence_count=0, top_score=0.2),
            QueryIntent.TOPIC,
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            "weak_retrieval_score",
        ),
        (
            _facts(facts_query_succeeded=False),
            QueryIntent.TOPIC,
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            "facts_query_failed",
        ),
    ],
)
def test_policy_is_table_driven_and_conservative(
    facts: CompletenessFacts,
    intent: QueryIntent,
    expected: CompletenessAction,
    reason: str,
) -> None:
    decision = DbFirstCompletenessPolicy(CompletenessConfig()).decide(
        facts=facts,
        intent=intent,
        remaining_deadline_seconds=10.0,
    )

    assert decision.action is expected
    assert reason in decision.reason_codes


def test_deadline_prevents_live_and_keeps_best_database_evidence() -> None:
    decision = DbFirstCompletenessPolicy(CompletenessConfig()).decide(
        facts=_facts(strong_evidence_count=0, top_score=0.2),
        intent=QueryIntent.TOPIC,
        remaining_deadline_seconds=0.5,
    )

    assert decision.action is CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING
    assert "deadline_insufficient" in decision.reason_codes


def test_policy_is_deterministic_and_respects_disabled_live_fallback() -> None:
    policy = DbFirstCompletenessPolicy(CompletenessConfig(live_fallback_enabled=False))
    facts = _facts(strong_evidence_count=0, top_score=0.2)

    first = policy.decide(
        facts=facts,
        intent=QueryIntent.TOPIC,
        remaining_deadline_seconds=10.0,
    )
    second = policy.decide(
        facts=facts,
        intent=QueryIntent.TOPIC,
        remaining_deadline_seconds=10.0,
    )

    assert first == second
    assert first.action is CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING
    assert "live_fallback_disabled" in first.reason_codes


@pytest.mark.parametrize(
    "config",
    [
        {"min_strong_evidence": 0},
        {"minimum_remaining_deadline_seconds": -0.1},
        {"live_fallback_max_details": 0},
        {"document_soft_stale_minutes": 361, "document_hard_stale_minutes": 360},
        {
            "announcement_soft_stale_minutes": 361,
            "announcement_hard_stale_minutes": 360,
        },
    ],
)
def test_policy_config_rejects_invalid_boundaries(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        CompletenessConfig(**config)


@pytest.mark.parametrize(
    "config",
    [
        {"live_fallback_max_pages": 6, "live_fallback_max_candidate_urls": 5},
        {
            "announcement_soft_stale_minutes": 361,
            "announcement_hard_stale_minutes": 360,
        },
    ],
)
def test_runtime_config_rejects_unsafe_live_fallback_boundaries(
    config: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        DbFirstCompletenessConfig.model_validate(config)
