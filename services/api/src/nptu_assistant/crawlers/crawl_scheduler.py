from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from uuid import UUID

from nptu_assistant.crawlers.site_map import SiteCrawlStatus

Jitter = Callable[[float], float]


@dataclass(frozen=True, slots=True)
class CrawlClaim:
    """A database-backed claim that a worker may act on."""

    page_id: UUID
    canonical_url: str
    owner: str
    token: UUID
    lease_expires_at: datetime
    crawl_priority: int
    next_crawl_at: datetime | None
    failure_count: int
    host: str = ""
    page_type: str = "unknown"
    unit: str | None = None
    minimum_depth: int = 0
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class AdaptiveScheduleConfig:
    success_interval_seconds: float = 3600.0
    unchanged_interval_seconds: float = 7200.0
    minimum_interval_seconds: float = 60.0
    maximum_interval_seconds: float = 7 * 24 * 3600.0
    retry_base_seconds: float = 60.0
    retry_factor: float = 2.0
    retry_max_seconds: float = 24 * 3600.0
    jitter_ratio: float = 0.10
    not_found_permanent_after: int = 3

    def __post_init__(self) -> None:
        if self.success_interval_seconds <= 0:
            raise ValueError("成功排程間隔必須大於零")
        if self.unchanged_interval_seconds <= 0:
            raise ValueError("未變更排程間隔必須大於零")
        if self.minimum_interval_seconds <= 0:
            raise ValueError("最小排程間隔必須大於零")
        if self.maximum_interval_seconds < self.minimum_interval_seconds:
            raise ValueError("最大排程間隔不得小於最小排程間隔")
        if self.retry_base_seconds <= 0 or self.retry_max_seconds <= 0:
            raise ValueError("重試間隔必須大於零")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("重試最大間隔不得小於基礎間隔")
        if self.retry_factor < 1:
            raise ValueError("重試倍率不得小於一")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter 比例必須介於零與一之間")
        if self.not_found_permanent_after < 1:
            raise ValueError("404/410 permanent threshold 必須大於零")


@dataclass(frozen=True, slots=True)
class FailureDecision:
    retry: bool
    crawl_status: str
    next_crawl_at: datetime | None
    delay_seconds: float | None
    deactivate: bool
    reason: str
    applied: bool | None = None


def parse_retry_after(
    value: str | None,
    *,
    now: datetime,
) -> float | None:
    """Parse Retry-After as seconds or an HTTP date.

    Invalid, negative, or timezone-less values are ignored.  The caller owns
    the upper bound because the appropriate cap is a scheduler policy choice.
    """

    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            return None
        seconds = (retry_at - now).total_seconds()
    if seconds < 0:
        return None
    return seconds


class CrawlLeaseRepository(Protocol):
    def claim_due(
        self,
        *,
        owner: str,
        limit: int,
        lease_duration: timedelta,
        now: datetime,
    ) -> tuple[CrawlClaim, ...]:
        raise NotImplementedError

    def renew(
        self,
        claim: CrawlClaim,
        *,
        lease_duration: timedelta,
        now: datetime,
    ) -> bool:
        raise NotImplementedError

    def complete(
        self,
        claim: CrawlClaim,
        *,
        crawl_status: str,
        next_crawl_at: datetime,
        now: datetime,
        http_status: int | None = None,
        content_changed: bool | None = None,
        links_discovered: int = 0,
        ingestion_performed: bool = False,
        outcome: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        raise NotImplementedError

    def fail(
        self,
        claim: CrawlClaim,
        *,
        decision: FailureDecision,
        http_status: int | None,
        now: datetime,
        error_kind: str | None = None,
        error_message: str | None = None,
        retry_after: str | None = None,
    ) -> bool:
        raise NotImplementedError


class AdaptiveSchedulePolicy:
    """Pure scheduling policy used by the DB repository and worker domain."""

    def __init__(
        self,
        config: AdaptiveScheduleConfig | None = None,
        *,
        jitter: Jitter | None = None,
    ) -> None:
        self.config = config or AdaptiveScheduleConfig()
        self._jitter = jitter or self._random_jitter

    def next_success_at(
        self,
        *,
        now: datetime,
        changed: bool,
        crawl_priority: int = 0,
    ) -> datetime:
        base = (
            self.config.success_interval_seconds
            if changed
            else self.config.unchanged_interval_seconds
        )
        # High-priority pages are checked more often while retaining a lower
        # bound so an unusually high priority cannot create a hot loop.
        priority_factor = max(0.25, 1.0 - max(crawl_priority, 0) / 200.0)
        delay = min(
            self.config.maximum_interval_seconds,
            max(self.config.minimum_interval_seconds, base * priority_factor),
        )
        return now + timedelta(seconds=self._jittered(delay))

    def failure_decision(
        self,
        *,
        now: datetime,
        http_status: int | None,
        failure_count: int,
        retry_after: str | None = None,
    ) -> FailureDecision:
        if (
            http_status in {404, 410}
            and failure_count >= self.config.not_found_permanent_after
        ):
            return FailureDecision(
                retry=False,
                crawl_status=SiteCrawlStatus.EXCLUDED.value,
                next_crawl_at=None,
                delay_seconds=None,
                deactivate=True,
                reason=f"HTTP {http_status}：資源不存在或已移除",
            )
        if http_status in {401, 403}:
            return FailureDecision(
                retry=False,
                crawl_status=SiteCrawlStatus.BLOCKED.value,
                next_crawl_at=None,
                delay_seconds=None,
                deactivate=True,
                reason=f"HTTP {http_status}：來源拒絕存取",
            )
        if (
            http_status is not None
            and 400 <= http_status < 500
            and http_status not in {404, 410, 429}
        ):
            return FailureDecision(
                retry=False,
                crawl_status=SiteCrawlStatus.EXCLUDED.value,
                next_crawl_at=None,
                delay_seconds=None,
                deactivate=True,
                reason=f"HTTP {http_status}：不可重試的用戶端錯誤",
            )

        server_delay = (
            parse_retry_after(retry_after, now=now)
            if http_status in {429, 503}
            else None
        )
        if server_delay is not None:
            delay = min(self.config.retry_max_seconds, server_delay)
        else:
            exponent = max(failure_count - 1, 0)
            delay = min(
                self.config.retry_max_seconds,
                self.config.retry_base_seconds * self.config.retry_factor**exponent,
            )
            delay = self._jittered(delay)
        return FailureDecision(
            retry=True,
            crawl_status=SiteCrawlStatus.FAILED.value,
            next_crawl_at=now + timedelta(seconds=delay),
            delay_seconds=delay,
            deactivate=False,
            reason=(
                "HTTP Retry-After：依伺服器要求延後重試"
                if http_status in {429, 503} and server_delay is not None
                else "暫時性抓取錯誤：採指數退避重試"
            ),
        )

    def _jittered(self, delay: float) -> float:
        jittered = self._jitter(delay)
        if jittered < 0:
            raise ValueError("jitter 不得產生負數延遲")
        return jittered

    def _random_jitter(self, delay: float) -> float:
        width = delay * self.config.jitter_ratio
        return random.uniform(max(0.0, delay - width), delay + width)


class CrawlScheduler:
    """Coordinates adaptive scheduling decisions with a lease repository."""

    def __init__(
        self,
        repository: CrawlLeaseRepository,
        *,
        policy: AdaptiveSchedulePolicy | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._policy = policy or AdaptiveSchedulePolicy()
        self._now = now

    def claim_due(
        self,
        *,
        owner: str,
        limit: int = 1,
        lease_duration: timedelta = timedelta(minutes=5),
    ) -> tuple[CrawlClaim, ...]:
        return self._repository.claim_due(
            owner=owner,
            limit=limit,
            lease_duration=lease_duration,
            now=self._now(),
        )

    def renew(self, claim: CrawlClaim, *, lease_duration: timedelta) -> bool:
        return self._repository.renew(
            claim,
            lease_duration=lease_duration,
            now=self._now(),
        )

    def complete(
        self,
        claim: CrawlClaim,
        *,
        changed: bool,
        http_status: int | None = None,
        links_discovered: int = 0,
        ingestion_performed: bool = False,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> bool:
        now = self._now()
        next_crawl_at = self._policy.next_success_at(
            now=now,
            changed=changed,
            crawl_priority=claim.crawl_priority,
        )
        return self._repository.complete(
            claim,
            crawl_status=(
                SiteCrawlStatus.SUCCESS.value
                if changed
                else SiteCrawlStatus.UNCHANGED.value
            ),
            next_crawl_at=next_crawl_at,
            now=now,
            http_status=http_status,
            content_changed=changed,
            links_discovered=links_discovered,
            ingestion_performed=ingestion_performed,
            etag=etag,
            last_modified=last_modified,
            outcome="success_changed"
            if changed
            else ("not_modified" if http_status == 304 else "success_unchanged"),
        )

    def fail(
        self,
        claim: CrawlClaim,
        *,
        http_status: int | None = None,
        retry_after: str | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> FailureDecision:
        now = self._now()
        decision = self._policy.failure_decision(
            now=now,
            http_status=http_status,
            failure_count=claim.failure_count + 1,
            retry_after=retry_after,
        )
        applied = self._repository.fail(
            claim,
            decision=decision,
            http_status=http_status,
            now=now,
            error_kind=error_kind,
            error_message=error_message,
            retry_after=retry_after,
        )
        return replace(decision, applied=applied)
