"""Durable, non-blocking refresh scheduling for DB-first retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from nptu_assistant.crawlers.site_models import SearchDeadline


class DurablePageScheduler(Protocol):
    def schedule_pages(
        self,
        *,
        urls: tuple[str, ...] = (),
        unit: str | None = None,
        host: str | None = None,
        page_type: str | None = None,
        run_at: datetime | None = None,
        deadline: SearchDeadline | None = None,
    ) -> int: ...

    def schedule_announcement_sources(
        self,
        *,
        source_names: tuple[str, ...],
        deadline: SearchDeadline | None = None,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class RefreshScheduleResult:
    attempted: bool
    succeeded: bool
    target_count: int
    scheduled_count: int
    reason: str


class CompletenessRefreshScheduler:
    """Small adapter over the durable crawl frontier.

    It does not create workers, execute HTTP, or wait for a crawl.  The
    repository's conditional update is the idempotency/fencing boundary.
    """

    def __init__(
        self, scheduler: DurablePageScheduler, *, max_targets: int = 220
    ) -> None:
        if max_targets < 1:
            raise ValueError("max_targets 必須至少為 1")
        self._scheduler = scheduler
        self._max_targets = max_targets

    def schedule(
        self,
        *,
        urls: tuple[str, ...],
        source_names: tuple[str, ...] = (),
        unit: str | None,
        reason: str,
        deadline: SearchDeadline | None = None,
    ) -> RefreshScheduleResult:
        targets = tuple(dict.fromkeys(urls))
        sources = tuple(dict.fromkeys(source_names))
        target_count = len(targets) + len(sources)
        if not target_count:
            return RefreshScheduleResult(False, True, 0, 0, reason)
        # Do not silently report a partial schedule: callers either persist
        # every bounded target or report that no durable refresh was accepted.
        if target_count > self._max_targets:
            return RefreshScheduleResult(
                True,
                False,
                target_count,
                0,
                "refresh_target_limit_exceeded",
            )
        if deadline is not None and deadline.expired():
            return RefreshScheduleResult(
                True,
                False,
                target_count,
                0,
                "deadline_expired_before_schedule",
            )
        try:
            scheduled_pages = (
                self._scheduler.schedule_pages(
                    urls=targets,
                    unit=unit,
                    deadline=deadline,
                )
                if targets
                else 0
            )
            scheduled_sources = (
                self._scheduler.schedule_announcement_sources(
                    source_names=sources,
                    deadline=deadline,
                )
                if sources
                else 0
            )
            scheduled = scheduled_pages + scheduled_sources
        except Exception:
            return RefreshScheduleResult(True, False, target_count, 0, reason)
        if targets and scheduled_pages != len(targets):
            return RefreshScheduleResult(
                True,
                False,
                target_count,
                scheduled,
                "page_schedule_incomplete",
            )
        if sources and scheduled_sources != len(sources):
            return RefreshScheduleResult(
                True,
                False,
                target_count,
                scheduled,
                "source_schedule_incomplete",
            )
        # A zero-row conditional update means every target was unknown,
        # blocked, excluded, or protected by a live lease.  Do not claim a
        # background refresh was scheduled in that case.
        return RefreshScheduleResult(
            True,
            scheduled > 0,
            target_count,
            scheduled,
            reason,
        )
