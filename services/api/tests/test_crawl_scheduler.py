from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from nptu_assistant.crawlers.crawl_scheduler import (
    AdaptiveScheduleConfig,
    AdaptiveScheduleInputs,
    AdaptiveSchedulePolicy,
    CrawlClaim,
    CrawlScheduler,
    parse_retry_after,
)
from nptu_assistant.db.crawl_scheduler import due_pages_statement
from nptu_assistant.crawlers.site_map import FrontierPolicy
from sqlalchemy.dialects import postgresql

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def test_due_claim_sql_is_ordered_and_uses_skip_locked() -> None:
    sql = str(
        due_pages_statement(now=NOW, limit=3).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "FOR UPDATE OF site_pages SKIP LOCKED" in sql
    assert "crawl_priority DESC" in sql
    assert "next_crawl_at ASC NULLS FIRST" in sql


def test_due_claim_sql_is_host_fair_and_respects_active_cap() -> None:
    sql = str(
        due_pages_statement(
            now=NOW,
            limit=3,
            frontier_policy=FrontierPolicy(
                max_depth=2,
                per_host_due_cap=2,
                per_host_active_cap=1,
            ),
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "row_number() OVER (PARTITION BY frontier_candidate.host" in sql
    assert "frontier_active.crawl_lease_expires_at >" in sql
    assert "frontier_candidate.minimum_depth <= 2" in sql
    assert "least(2, 1 - ranked_frontier.active_count)" in sql
    assert "FOR UPDATE OF site_pages SKIP LOCKED" in sql


def test_retry_after_supports_seconds_and_http_date() -> None:
    assert parse_retry_after("30", now=NOW) == 30
    assert parse_retry_after("Sun, 02 Aug 2026 12:01:00 GMT", now=NOW) == 60
    assert parse_retry_after("-1", now=NOW) is None
    assert parse_retry_after("invalid", now=NOW) is None


def test_failure_policy_has_explicit_status_policies() -> None:
    policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(jitter_ratio=0),
        jitter=lambda delay: delay,
    )

    gone = policy.failure_decision(now=NOW, http_status=410, failure_count=1)
    forbidden = policy.failure_decision(now=NOW, http_status=403, failure_count=1)
    rate_limited = policy.failure_decision(
        now=NOW,
        http_status=429,
        failure_count=1,
        retry_after="90",
    )
    server_error = policy.failure_decision(now=NOW, http_status=503, failure_count=3)

    assert (gone.retry, gone.deactivate, gone.crawl_status) == (True, False, "failed")
    permanent = policy.failure_decision(
        now=NOW,
        http_status=410,
        failure_count=3,
    )
    assert (permanent.retry, permanent.deactivate, permanent.crawl_status) == (
        False,
        True,
        "excluded",
    )
    assert (forbidden.retry, forbidden.deactivate, forbidden.crawl_status) == (
        False,
        True,
        "blocked",
    )
    assert rate_limited.delay_seconds == 90
    assert rate_limited.next_crawl_at == NOW + timedelta(seconds=90)
    assert server_error.retry is True
    assert server_error.delay_seconds == 240


def test_success_interval_and_jitter_are_injectable() -> None:
    policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(
            success_interval_seconds=100,
            unchanged_interval_seconds=200,
            minimum_interval_seconds=1,
            jitter_ratio=1,
        ),
        jitter=lambda delay: delay + 7,
    )

    assert policy.next_success_at(
        now=NOW, changed=True, crawl_priority=0
    ) == NOW + timedelta(seconds=107)
    assert policy.next_success_at(
        now=NOW, changed=False, crawl_priority=0
    ) == NOW + timedelta(seconds=207)


def test_success_policy_is_deterministic_and_uses_all_persisted_inputs() -> None:
    policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(
            success_interval_seconds=100,
            unchanged_interval_seconds=100,
            minimum_interval_seconds=10,
            maximum_interval_seconds=250,
        )
    )
    inputs = AdaptiveScheduleInputs(
        page_type="announcement_listing",
        changed=False,
        changed_streak=0,
        unchanged_streak=4,
        crawl_priority=100,
        failure_count=2,
        retry_after="30",
    )

    first = policy.success_decision(now=NOW, inputs=inputs)
    second = policy.success_decision(now=NOW, inputs=inputs)

    assert first == second
    assert first.policy_inputs == inputs
    assert first.delay_seconds == 75
    assert first.next_crawl_at == NOW + timedelta(seconds=75)


def test_success_policy_respects_minimum_and_maximum_after_jitter() -> None:
    policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(
            success_interval_seconds=100,
            minimum_interval_seconds=30,
            maximum_interval_seconds=120,
        ),
        jitter=lambda delay: delay * 10,
    )

    assert policy.next_success_at(
        now=NOW,
        changed=True,
        page_type="search_result",
        crawl_priority=0,
    ) == NOW + timedelta(seconds=120)

    minimum_policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(
            success_interval_seconds=1,
            minimum_interval_seconds=30,
            maximum_interval_seconds=120,
        ),
        jitter=lambda delay: delay / 10,
    )
    assert minimum_policy.next_success_at(now=NOW, changed=True) == NOW + timedelta(
        seconds=30
    )


def test_failure_policy_uses_page_type_priority_streak_and_injected_jitter() -> None:
    policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(
            retry_base_seconds=100,
            retry_factor=2,
            retry_max_seconds=500,
            minimum_interval_seconds=10,
            maximum_interval_seconds=500,
        ),
        jitter=lambda delay: delay + 5,
    )

    decision = policy.failure_decision(
        now=NOW,
        http_status=503,
        failure_count=2,
        page_type="search_result",
        unchanged_streak=2,
        crawl_priority=0,
    )

    assert decision.retry is True
    assert decision.delay_seconds == 380
    assert decision.next_crawl_at == NOW + timedelta(seconds=380)
    assert decision.policy_inputs is not None
    assert decision.policy_inputs.page_type == "search_result"


def test_retry_after_is_parsed_and_bounded_without_jitter() -> None:
    policy = AdaptiveSchedulePolicy(
        AdaptiveScheduleConfig(
            minimum_interval_seconds=10,
            maximum_interval_seconds=100,
            retry_max_seconds=80,
        ),
        jitter=lambda delay: delay + 7,
    )

    assert (
        policy.failure_decision(
            now=NOW,
            http_status=429,
            failure_count=1,
            retry_after="1",
        ).delay_seconds
        == 10
    )
    assert (
        policy.failure_decision(
            now=NOW,
            http_status=429,
            failure_count=1,
            retry_after="500",
        ).delay_seconds
        == 80
    )


class RecordingRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def claim_due(self, **kwargs: object) -> tuple[CrawlClaim, ...]:
        self.calls.append(("claim", kwargs))
        return ()

    def renew(self, claim: CrawlClaim, **kwargs: object) -> bool:
        self.calls.append(("renew", (claim, kwargs)))
        return True

    def complete(self, claim: CrawlClaim, **kwargs: object) -> bool:
        self.calls.append(("complete", (claim, kwargs)))
        return True

    def fail(self, claim: CrawlClaim, **kwargs: object) -> bool:
        self.calls.append(("fail", (claim, kwargs)))
        return True


def test_scheduler_passes_fenced_claim_and_policy_decision_to_repository() -> None:
    repository = RecordingRepository()
    scheduler = CrawlScheduler(
        repository,
        policy=AdaptiveSchedulePolicy(jitter=lambda delay: delay),
        now=lambda: NOW,
    )
    claim = CrawlClaim(
        page_id=uuid4(),
        canonical_url="https://www.nptu.edu.tw/",
        owner="worker-a",
        token=uuid4(),
        lease_expires_at=NOW + timedelta(minutes=5),
        crawl_priority=100,
        next_crawl_at=NOW,
        failure_count=0,
        page_type="announcement_listing",
        changed_streak=2,
        unchanged_streak=3,
    )

    decision = scheduler.fail(claim, http_status=429, retry_after="120")
    assert decision.next_crawl_at == NOW + timedelta(seconds=120)
    assert decision.applied is True
    assert repository.calls[0][0] == "fail"
    failed_claim, kwargs = repository.calls[0][1]
    assert failed_claim.token == claim.token
    assert kwargs["decision"].delay_seconds == decision.delay_seconds
    assert kwargs["decision"].applied is None
    assert decision.policy_inputs is not None
    assert decision.policy_inputs.page_type == "announcement_listing"
    assert decision.policy_inputs.changed_streak == 2
    assert decision.policy_inputs.unchanged_streak == 3


def test_invalid_adaptive_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="jitter"):
        AdaptiveScheduleConfig(jitter_ratio=1.1)
