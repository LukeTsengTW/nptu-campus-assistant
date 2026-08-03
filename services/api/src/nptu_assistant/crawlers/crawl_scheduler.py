from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from nptu_assistant.crawlers.site_map import SiteCrawlStatus
from nptu_assistant.crawlers.site_models import SearchDeadline

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
    changed_streak: int = 0
    unchanged_streak: int = 0
    last_retry_after_at: datetime | None = None


def canonical_crawl_identity(claim: CrawlClaim) -> str:
    """Return the stable identity used to spread a page's crawl schedule."""

    return claim.canonical_url or f"page:{claim.page_id}"


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
    changed_streak_interval_factor: float = 0.10
    unchanged_streak_interval_factor: float = 0.25
    maximum_streak: int = 10
    page_type_interval_factors: tuple[tuple[str, float], ...] = (
        ("unit_homepage", 0.75),
        ("announcement_listing", 0.75),
        ("announcement_detail", 0.85),
        ("official_document", 1.0),
        ("general_page", 1.0),
        ("search_result", 1.25),
        ("unknown", 1.0),
    )

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
        if self.retry_max_seconds < self.minimum_interval_seconds:
            raise ValueError("重試最大間隔不得小於最小排程間隔")
        if self.retry_factor < 1:
            raise ValueError("重試倍率不得小於一")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter 比例必須介於零與一之間")
        if self.not_found_permanent_after < 1:
            raise ValueError("404/410 permanent threshold 必須大於零")
        if self.changed_streak_interval_factor < 0:
            raise ValueError("changed streak 調整比例不得為負數")
        if self.unchanged_streak_interval_factor < 0:
            raise ValueError("unchanged streak 調整比例不得為負數")
        if self.maximum_streak < 1:
            raise ValueError("streak 上限必須大於零")
        factors = dict(self.page_type_interval_factors)
        if len(factors) != len(self.page_type_interval_factors):
            raise ValueError("page type 排程倍率不得重複")
        if any(not page_type or factor <= 0 for page_type, factor in factors.items()):
            raise ValueError("page type 排程倍率必須為正數")


@dataclass(frozen=True, slots=True)
class AdaptiveScheduleInputs:
    """All persisted and response-derived inputs used by the policy.

    The repository supplies the persisted fields in one claim query.  The
    response-only fields (``changed`` and ``retry_after``) are supplied by
    the worker when it reports the outcome.
    """

    page_type: str = "unknown"
    changed: bool | None = None
    changed_streak: int = 0
    unchanged_streak: int = 0
    crawl_priority: int = 0
    failure_count: int = 0
    retry_after: str | None = None
    identity: str = ""

    def __post_init__(self) -> None:
        normalized_page_type = getattr(self.page_type, "value", self.page_type)
        object.__setattr__(
            self,
            "page_type",
            str(normalized_page_type or "unknown"),
        )
        object.__setattr__(self, "identity", str(self.identity or "").strip())
        for name in ("changed_streak", "unchanged_streak", "failure_count"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} 不得為負數")

    @property
    def priority(self) -> int:
        """Backward-compatible short name for the crawl priority input."""

        return self.crawl_priority


@dataclass(frozen=True, slots=True)
class FailureDecision:
    retry: bool
    crawl_status: str
    next_crawl_at: datetime | None
    delay_seconds: float | None
    deactivate: bool
    reason: str
    applied: bool | None = None
    policy_inputs: AdaptiveScheduleInputs | None = None


@dataclass(frozen=True, slots=True)
class SuccessDecision:
    next_crawl_at: datetime
    delay_seconds: float
    reason: str
    policy_inputs: AdaptiveScheduleInputs


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
        urls: tuple[str, ...] = (),
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
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
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
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        ingestion_performed: bool = False,
    ) -> bool:
        raise NotImplementedError

    def schedule_pages(
        self,
        *,
        urls: tuple[str, ...] = (),
        unit: str | None = None,
        host: str | None = None,
        page_type: str | None = None,
        run_at: datetime | None = None,
        deadline: SearchDeadline | None = None,
    ) -> int:
        raise NotImplementedError

    def schedule_announcement_sources(
        self,
        *,
        source_names: tuple[str, ...],
        deadline: SearchDeadline | None = None,
    ) -> int:
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
        # A supplied jitter remains an escape hatch for deterministic tests or
        # deployments with their own policy.  The production default below is
        # derived from the page identity rather than process-local hash state.
        self._jitter = jitter

    def success_decision(
        self,
        *,
        now: datetime,
        changed: bool | None = None,
        page_type: str = "unknown",
        changed_streak: int = 0,
        unchanged_streak: int = 0,
        crawl_priority: int = 0,
        failure_count: int = 0,
        retry_after: str | None = None,
        identity: str | None = None,
        priority: int | None = None,
        inputs: AdaptiveScheduleInputs | None = None,
    ) -> SuccessDecision:
        policy_inputs = self._coerce_inputs(
            inputs=inputs,
            changed=changed,
            page_type=page_type,
            changed_streak=changed_streak,
            unchanged_streak=unchanged_streak,
            crawl_priority=crawl_priority if priority is None else priority,
            failure_count=failure_count,
            retry_after=retry_after,
            identity=identity,
        )
        if policy_inputs.changed is None:
            raise ValueError("成功排程必須提供 changed")

        base = (
            self.config.success_interval_seconds
            if policy_inputs.changed
            else self.config.unchanged_interval_seconds
        )
        delay = base * self._adaptive_factor(policy_inputs)
        delay = self._bounded(delay, self.config.maximum_interval_seconds)
        delay = self._jittered(
            delay,
            maximum=self.config.maximum_interval_seconds,
            identity=self._state_identity(policy_inputs, outcome="success"),
        )
        return SuccessDecision(
            next_crawl_at=now + timedelta(seconds=delay),
            delay_seconds=delay,
            reason=(
                "成功後依 page type、streak、priority 與 failure 狀態排程"
                if policy_inputs.changed
                else "內容未變更，依 page type、streak、priority 與 failure 狀態排程"
            ),
            policy_inputs=policy_inputs,
        )

    def next_success_at(
        self,
        *,
        now: datetime,
        changed: bool | None = None,
        page_type: str = "unknown",
        changed_streak: int = 0,
        unchanged_streak: int = 0,
        crawl_priority: int = 0,
        failure_count: int = 0,
        retry_after: str | None = None,
        identity: str | None = None,
        priority: int | None = None,
        inputs: AdaptiveScheduleInputs | None = None,
    ) -> datetime:
        return self.success_decision(
            now=now,
            changed=changed,
            page_type=page_type,
            changed_streak=changed_streak,
            unchanged_streak=unchanged_streak,
            crawl_priority=crawl_priority,
            failure_count=failure_count,
            retry_after=retry_after,
            identity=identity,
            priority=priority,
            inputs=inputs,
        ).next_crawl_at

    def failure_decision(
        self,
        *,
        now: datetime,
        http_status: int | None,
        failure_count: int | None = None,
        retry_after: str | None = None,
        page_type: str = "unknown",
        changed_streak: int = 0,
        unchanged_streak: int = 0,
        crawl_priority: int = 0,
        identity: str | None = None,
        priority: int | None = None,
        inputs: AdaptiveScheduleInputs | None = None,
    ) -> FailureDecision:
        policy_inputs = self._coerce_inputs(
            inputs=inputs,
            changed=None,
            page_type=page_type,
            changed_streak=changed_streak,
            unchanged_streak=unchanged_streak,
            crawl_priority=crawl_priority if priority is None else priority,
            failure_count=0 if failure_count is None else failure_count,
            retry_after=retry_after,
            identity=identity,
        )
        if (
            http_status in {404, 410}
            and policy_inputs.failure_count >= self.config.not_found_permanent_after
        ):
            return FailureDecision(
                retry=False,
                crawl_status=SiteCrawlStatus.EXCLUDED.value,
                next_crawl_at=None,
                delay_seconds=None,
                deactivate=True,
                reason=f"HTTP {http_status}：資源不存在或已移除",
                policy_inputs=policy_inputs,
            )
        if http_status in {401, 403}:
            return FailureDecision(
                retry=False,
                crawl_status=SiteCrawlStatus.BLOCKED.value,
                next_crawl_at=None,
                delay_seconds=None,
                deactivate=True,
                reason=f"HTTP {http_status}：來源拒絕存取",
                policy_inputs=policy_inputs,
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
                policy_inputs=policy_inputs,
            )

        server_delay = (
            parse_retry_after(policy_inputs.retry_after, now=now)
            if http_status in {429, 503}
            else None
        )
        if server_delay is not None:
            # A valid Retry-After is a server directive: cap it for bounded
            # recovery, but do not add policy jitter that could retry early.
            delay = self._bounded(
                server_delay,
                min(
                    self.config.retry_max_seconds,
                    self.config.maximum_interval_seconds,
                ),
            )
        else:
            exponent = max(policy_inputs.failure_count - 1, 0)
            delay = self.config.retry_base_seconds * self.config.retry_factor**exponent
            delay *= self._adaptive_factor(policy_inputs)
            delay = self._bounded(
                delay,
                min(
                    self.config.retry_max_seconds,
                    self.config.maximum_interval_seconds,
                ),
            )
            delay = self._jittered(
                delay,
                maximum=min(
                    self.config.retry_max_seconds,
                    self.config.maximum_interval_seconds,
                ),
                identity=self._state_identity(
                    policy_inputs, outcome=f"failure:{http_status or 'unknown'}"
                ),
            )
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
            policy_inputs=policy_inputs,
        )

    def _coerce_inputs(
        self,
        *,
        inputs: AdaptiveScheduleInputs | None,
        changed: bool | None,
        page_type: str,
        changed_streak: int,
        unchanged_streak: int,
        crawl_priority: int,
        failure_count: int,
        retry_after: str | None,
        identity: str | None,
    ) -> AdaptiveScheduleInputs:
        if inputs is not None:
            if changed is not None and inputs.changed not in {None, changed}:
                raise ValueError("changed 與 policy inputs 不一致")
            if retry_after is not None and inputs.retry_after not in {
                None,
                retry_after,
            }:
                raise ValueError("Retry-After 與 policy inputs 不一致")
            if identity is not None and inputs.identity not in {"", identity}:
                raise ValueError("identity 與 policy inputs 不一致")
            if identity is not None and not inputs.identity:
                return replace(inputs, identity=identity)
            return inputs
        return AdaptiveScheduleInputs(
            page_type=page_type,
            changed=changed,
            changed_streak=changed_streak,
            unchanged_streak=unchanged_streak,
            crawl_priority=crawl_priority,
            failure_count=failure_count,
            retry_after=retry_after,
            identity=identity or "",
        )

    def _adaptive_factor(self, inputs: AdaptiveScheduleInputs) -> float:
        page_type_factor = dict(self.config.page_type_interval_factors).get(
            inputs.page_type,
            1.0,
        )
        # High-priority pages are checked more often while retaining a lower
        # bound so an unusually high priority cannot create a hot loop.
        priority_factor = max(0.25, 1.0 - max(inputs.crawl_priority, 0) / 200.0)
        changed_streak = min(inputs.changed_streak, self.config.maximum_streak)
        unchanged_streak = min(inputs.unchanged_streak, self.config.maximum_streak)
        if inputs.changed is True:
            streak_factor = 1.0 / (
                1.0 + self.config.changed_streak_interval_factor * changed_streak
            )
        elif inputs.changed is False:
            streak_factor = 1.0 + (
                self.config.unchanged_streak_interval_factor * unchanged_streak
            )
        elif changed_streak >= unchanged_streak:
            streak_factor = 1.0 / (
                1.0 + self.config.changed_streak_interval_factor * changed_streak
            )
        else:
            streak_factor = 1.0 + (
                self.config.unchanged_streak_interval_factor * unchanged_streak
            )
        return page_type_factor * priority_factor * streak_factor

    def _bounded(self, delay: float, maximum: float) -> float:
        return min(
            maximum,
            max(self.config.minimum_interval_seconds, delay),
        )

    def _jittered(
        self,
        delay: float,
        *,
        maximum: float,
        identity: str,
    ) -> float:
        jittered = (
            self._jitter(delay)
            if self._jitter is not None
            else self._stable_jitter(
                delay,
                identity=identity,
                jitter_ratio=self.config.jitter_ratio,
            )
        )
        if jittered < 0:
            raise ValueError("jitter 不得產生負數延遲")
        return self._bounded(jittered, maximum)

    @staticmethod
    def _state_identity(inputs: AdaptiveScheduleInputs, *, outcome: str) -> str:
        """Build a stable, state-aware jitter key without process-local hash()."""

        if not inputs.identity:
            return ""
        return "|".join(
            (
                inputs.identity,
                f"page_type={inputs.page_type}",
                f"changed={inputs.changed}",
                f"changed_streak={inputs.changed_streak}",
                f"unchanged_streak={inputs.unchanged_streak}",
                f"failure_count={inputs.failure_count}",
                f"priority={inputs.crawl_priority}",
                f"outcome={outcome}",
            )
        )

    @staticmethod
    def _stable_jitter(
        delay: float,
        *,
        identity: str,
        jitter_ratio: float,
    ) -> float:
        if not identity or jitter_ratio == 0:
            return delay
        digest = sha256(identity.encode("utf-8")).digest()
        unit = int.from_bytes(digest[:8], "big") / float(1 << 64)
        centered = (unit * 2.0) - 1.0
        return delay * (1.0 + centered * jitter_ratio)


class CrawlScheduler:
    """Coordinates adaptive scheduling decisions with a lease repository."""

    def __init__(
        self,
        repository: CrawlLeaseRepository,
        *,
        policy: AdaptiveSchedulePolicy | None = None,
        identity: Callable[[CrawlClaim], str] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._policy = policy or AdaptiveSchedulePolicy()
        self._identity = identity or canonical_crawl_identity
        self._now = now

    def claim_due(
        self,
        *,
        owner: str,
        limit: int = 1,
        lease_duration: timedelta = timedelta(minutes=5),
        urls: tuple[str, ...] = (),
    ) -> tuple[CrawlClaim, ...]:
        return self._repository.claim_due(
            owner=owner,
            limit=limit,
            lease_duration=lease_duration,
            now=self._now(),
            urls=urls,
        )

    def schedule_pages(
        self,
        *,
        urls: tuple[str, ...] = (),
        unit: str | None = None,
        host: str | None = None,
        page_type: str | None = None,
        run_at: datetime | None = None,
        deadline: SearchDeadline | None = None,
    ) -> int:
        """Persist explicit targets before claiming them.

        This keeps manual/targeted runs on the same durable frontier and host
        fairness path as normal due-page work.
        """

        result = self._repository.schedule_pages(
            urls=urls,
            unit=unit,
            host=host,
            page_type=page_type,
            run_at=run_at or self._now(),
            deadline=deadline,
        )
        return result

    def schedule_announcement_sources(
        self,
        *,
        source_names: tuple[str, ...],
        deadline: SearchDeadline | None = None,
    ) -> int:
        return self._repository.schedule_announcement_sources(
            source_names=source_names,
            deadline=deadline,
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
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
    ) -> bool:
        now = self._now()
        next_crawl_at = self._policy.next_success_at(
            now=now,
            changed=changed,
            page_type=claim.page_type,
            changed_streak=claim.changed_streak,
            unchanged_streak=claim.unchanged_streak,
            crawl_priority=claim.crawl_priority,
            failure_count=claim.failure_count,
            identity=self._identity(claim),
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
            content_type=content_type,
            content_length=content_length,
            final_url=final_url,
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
        content_type: str | None = None,
        content_length: int | None = None,
        final_url: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        ingestion_performed: bool = False,
    ) -> FailureDecision:
        now = self._now()
        decision = self._policy.failure_decision(
            now=now,
            http_status=http_status,
            failure_count=claim.failure_count + 1,
            retry_after=retry_after,
            page_type=claim.page_type,
            changed_streak=claim.changed_streak,
            unchanged_streak=claim.unchanged_streak,
            crawl_priority=claim.crawl_priority,
            identity=self._identity(claim),
        )
        applied = self._repository.fail(
            claim,
            decision=decision,
            http_status=http_status,
            now=now,
            error_kind=error_kind,
            error_message=error_message,
            retry_after=retry_after,
            content_type=content_type,
            content_length=content_length,
            final_url=final_url,
            etag=etag,
            last_modified=last_modified,
            ingestion_performed=ingestion_performed,
        )
        return replace(decision, applied=applied)
