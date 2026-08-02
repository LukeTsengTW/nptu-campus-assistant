from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from nptu_assistant.crawlers.crawl_scheduler import (
    AdaptiveScheduleConfig,
    AdaptiveSchedulePolicy,
    CrawlClaim,
    CrawlScheduler,
    parse_retry_after,
)
from nptu_assistant.db.crawl_scheduler import due_pages_statement
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
    )

    decision = scheduler.fail(claim, http_status=429, retry_after="120")
    assert decision.next_crawl_at == NOW + timedelta(seconds=120)
    assert decision.applied is True
    assert repository.calls[0][0] == "fail"
    failed_claim, kwargs = repository.calls[0][1]
    assert failed_claim.token == claim.token
    assert kwargs["decision"].delay_seconds == decision.delay_seconds
    assert kwargs["decision"].applied is None


def test_invalid_adaptive_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="jitter"):
        AdaptiveScheduleConfig(jitter_ratio=1.1)
