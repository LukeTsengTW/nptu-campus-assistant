from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast

from nptu_assistant.api.errors import AppError
from nptu_assistant.api.schemas import (
    CrawlScheduleRequest,
    CrawlScheduleResponse,
    CrawlStatusResponse,
)

logger = logging.getLogger(__name__)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _datetime_value(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _text_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, str)]


class RefreshCoordinator(Protocol):
    def refresh_due_sources(self) -> list[object]:
        """Refresh all due sources without coupling the API to crawler internals."""
        ...

    def ensure_fresh(self, source_name: str) -> object:
        """Refresh one source when a manually scheduled request names it."""
        ...


class CrawlAdminControl(Protocol):
    def status(self) -> Mapping[str, object]: ...

    def schedule(self, request: CrawlScheduleRequest) -> Mapping[str, object]: ...


class CrawlWorker(Protocol):
    def run_once(
        self,
        *,
        source_names: Sequence[str] | None = None,
        dry_run: bool = False,
    ) -> Mapping[str, object]: ...

    async def run_loop(self, *, dry_run: bool = False) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _PendingSchedule:
    schedule_id: str
    scheduled_at: datetime
    run_at: datetime
    source_names: tuple[str, ...]
    dry_run: bool


class CrawlWorkerController:
    """Shared worker control plane for the API lifespan and the CLI worker."""

    def __init__(
        self,
        coordinator: RefreshCoordinator,
        *,
        source_names: Callable[[], Sequence[str]] | None = None,
        interval_seconds: float = 60.0,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        schedule_store: object | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("worker interval 必須大於 0")
        self._coordinator = coordinator
        self._source_names = source_names or (lambda: ())
        self._interval_seconds = interval_seconds
        self._now = now
        self._schedule_store = schedule_store
        self._lock = threading.Lock()
        self._stop_requested = False
        self._running = False
        self._pending: _PendingSchedule | None = None
        self._stop_event: asyncio.Event | None = None
        self._wake_event: asyncio.Event | None = None
        self._last_report: dict[str, object] | None = None
        self._last_success_at: datetime | None = None
        self._runs_total = 0
        self._successes_total = 0
        self._failures_total = 0
        self._schedules_total = 0

    def status(self) -> Mapping[str, object]:
        with self._lock:
            if self._running:
                state = "running"
            elif self._stop_requested:
                state = "stopped"
            else:
                state = "idle"
            report = _mapping(dict(self._last_report or {}))
            pending = self._pending
            durable = {}
            status = getattr(self._schedule_store, "status", None)
            if callable(status):
                durable = _mapping(status(now=self._now()))
            return CrawlStatusResponse(
                status=state,
                enabled=True,
                interval_seconds=self._interval_seconds,
                run_id=_text(report.get("run_id")),
                last_run_at=_datetime_value(report.get("finished_at")),
                last_success_at=self._last_success_at,
                last_error=(_text_list(report.get("errors")) or [None])[0],
                runs_total=self._runs_total,
                successes_total=self._successes_total,
                failures_total=self._failures_total,
                schedules_total=self._schedules_total,
                queue_depth=int(pending is not None),
                last_run_status=_text(report.get("status")),
                last_sources_attempted=_integer(report.get("sources_attempted")),
                last_sources_succeeded=_integer(report.get("sources_succeeded")),
                last_sources_failed=_integer(report.get("sources_failed")),
                last_created=_integer(report.get("created")),
                last_updated=_integer(report.get("updated")),
                last_unchanged=_integer(report.get("unchanged")),
                last_failed=_integer(report.get("failed")),
                last_errors=_text_list(report.get("errors")),
                last_duration_ms=_number(report.get("duration_ms")),
                next_run_at=pending.run_at if pending else None,
                pending_schedule_id=pending.schedule_id if pending else None,
                dry_run=bool(report.get("dry_run", False)),
                due=_integer(durable.get("due")),
                leased=_integer(durable.get("leased")),
                blocked=_integer(durable.get("blocked")),
                active_workers=_integer(durable.get("active_workers")),
                next_due_at=_datetime_value(durable.get("next_due_at")),
            ).model_dump(mode="json")

    def schedule(self, request: CrawlScheduleRequest) -> Mapping[str, object]:
        now = self._as_utc(self._now())
        run_at = request.run_at
        if run_at is None:
            run_at = now + timedelta(seconds=request.delay_seconds)
        else:
            run_at = self._as_utc(run_at)
        pending = _PendingSchedule(
            schedule_id=str(uuid.uuid4()),
            scheduled_at=now,
            run_at=run_at,
            source_names=tuple(request.source_names or ()),
            dry_run=request.dry_run,
        )
        scheduled_pages = 0
        schedule_pages = cast(
            Callable[..., int] | None,
            getattr(self._schedule_store, "schedule_pages", None),
        )
        if callable(schedule_pages) and (
            request.urls or request.unit or request.host or request.page_type
        ):
            scheduled_pages = int(
                schedule_pages(
                    urls=tuple(request.urls or ()),
                    unit=request.unit,
                    host=request.host,
                    page_type=request.page_type,
                    run_at=run_at,
                )
            )
        with self._lock:
            self._pending = pending
            self._schedules_total += 1
            wake_event = self._wake_event
        if wake_event is not None:
            wake_event.set()
        logger.info(
            "crawl_schedule_accepted",
            extra={
                "schedule_id": pending.schedule_id,
                "run_at": pending.run_at.isoformat(),
                "source_count": len(pending.source_names),
                "dry_run": pending.dry_run,
            },
        )
        return CrawlScheduleResponse(
            status="scheduled",
            schedule_id=pending.schedule_id,
            scheduled_at=pending.scheduled_at,
            run_at=pending.run_at,
            source_names=list(pending.source_names),
            dry_run=pending.dry_run,
            scheduled_pages=scheduled_pages,
        ).model_dump(mode="json")

    def run_once(
        self,
        *,
        source_names: Sequence[str] | None = None,
        dry_run: bool = False,
    ) -> Mapping[str, object]:
        started_at = self._as_utc(self._now())
        run_id = str(uuid.uuid4())
        selected = tuple(source_names or ())
        if dry_run and not selected:
            selected = tuple(self._source_names())
        with self._lock:
            self._running = True
        try:
            if dry_run:
                results: list[object] = []
            elif selected:
                ensure_fresh = getattr(self._coordinator, "ensure_fresh", None)
                if not callable(ensure_fresh):
                    raise RuntimeError("refresh coordinator 不支援指定來源排程")
                results = [ensure_fresh(name) for name in selected]
            else:
                results = list(self._coordinator.refresh_due_sources())
            report = self._build_report(
                run_id=run_id,
                started_at=started_at,
                source_names=selected,
                results=results,
                dry_run=dry_run,
            )
        except Exception as exc:
            logger.exception("crawl worker execution failed", extra={"run_id": run_id})
            finished_at = self._as_utc(self._now())
            report = {
                "run_id": run_id,
                "status": "failed",
                "dry_run": dry_run,
                "source_names": list(selected),
                "sources_attempted": 0,
                "sources_succeeded": 0,
                "sources_failed": 1,
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "failed": 1,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": max(
                    0.0, (finished_at - started_at).total_seconds() * 1000
                ),
            }
        finally:
            with self._lock:
                self._running = False
        with self._lock:
            self._last_report = dict(report)
            self._runs_total += 1
            if report["status"] == "completed" or report["status"] == "dry_run":
                self._successes_total += 1
                if report["status"] == "completed":
                    self._last_success_at = report["finished_at"]  # type: ignore[assignment]
            else:
                self._failures_total += 1
        logger.info(
            "crawl_run_complete",
            extra={
                "crawl_metrics": {
                    key: value
                    for key, value in report.items()
                    if key not in {"errors", "source_names"}
                }
            },
        )
        return report

    async def run_loop(self, *, dry_run: bool = False) -> None:
        stop_event = asyncio.Event()
        wake_event = asyncio.Event()
        with self._lock:
            self._stop_event = stop_event
            self._wake_event = wake_event
            self._stop_requested = False
        next_periodic = self._as_utc(self._now())
        try:
            while not stop_event.is_set():
                now = self._as_utc(self._now())
                pending = self._take_due_schedule(now)
                if pending is not None:
                    await asyncio.to_thread(
                        self.run_once,
                        source_names=pending.source_names or None,
                        dry_run=pending.dry_run,
                    )
                    next_periodic = self._as_utc(self._now()) + timedelta(
                        seconds=self._interval_seconds
                    )
                    continue
                if now >= next_periodic:
                    await asyncio.to_thread(self.run_once, dry_run=dry_run)
                    next_periodic = self._as_utc(self._now()) + timedelta(
                        seconds=self._interval_seconds
                    )
                    continue
                wait_seconds = max(0.0, (next_periodic - now).total_seconds())
                with self._lock:
                    if self._pending is not None:
                        wait_seconds = min(
                            wait_seconds,
                            max(0.0, (self._pending.run_at - now).total_seconds()),
                        )
                stop_task = asyncio.create_task(stop_event.wait())
                wake_task = asyncio.create_task(wake_event.wait())
                done, pending_tasks = await asyncio.wait(
                    {stop_task, wake_task},
                    timeout=wait_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending_tasks:
                    task.cancel()
                if pending_tasks:
                    await asyncio.gather(*pending_tasks, return_exceptions=True)
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
                wake_event.clear()
        finally:
            with self._lock:
                self._stop_event = None
                self._wake_event = None

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()

    def _take_due_schedule(self, now: datetime) -> _PendingSchedule | None:
        with self._lock:
            if self._pending is None or self._pending.run_at > now:
                return None
            pending = self._pending
            self._pending = None
            return pending

    def _build_report(
        self,
        *,
        run_id: str,
        started_at: datetime,
        source_names: Sequence[str],
        results: Sequence[object],
        dry_run: bool,
    ) -> dict[str, object]:
        counters = {"created": 0, "updated": 0, "unchanged": 0, "failed": 0}
        sources_attempted = 0
        sources_succeeded = 0
        sources_failed = 0
        errors: list[str] = []
        for result in results:
            attempted = bool(getattr(result, "attempted", True))
            succeeded = bool(
                getattr(result, "succeeded", not getattr(result, "summary", None))
            )
            sources_attempted += int(attempted)
            sources_succeeded += int(succeeded)
            sources_failed += int(not succeeded)
            summary = getattr(result, "summary", None)
            if summary is not None:
                for key in counters:
                    counters[key] += _integer(getattr(summary, key, 0))
                errors.extend(_text_list(getattr(summary, "errors", [])))
            counts = getattr(result, "counts", None)
            if callable(counts):
                counts = counts()
            if isinstance(counts, Mapping):
                counters["created"] += int(
                    sum(
                        _integer(value)
                        for key, value in counts.items()
                        if str(key) == "changed"
                    )
                )
                counters["unchanged"] += int(
                    sum(
                        _integer(value)
                        for key, value in counts.items()
                        if str(key) in {"unchanged", "lease_not_acquired"}
                    )
                )
                counters["failed"] += int(
                    sum(
                        _integer(value)
                        for key, value in counts.items()
                        if str(key)
                        in {
                            "failed",
                            "blocked",
                            "binary",
                            "unsupported",
                            "lease_lost",
                            "ingestion_failed",
                        }
                    )
                )
            warning = getattr(result, "warning", None)
            if warning:
                errors.append(str(warning))
        finished_at = self._as_utc(self._now())
        return {
            "run_id": run_id,
            "status": "dry_run"
            if dry_run
            else ("failed" if sources_failed else "completed"),
            "dry_run": dry_run,
            "source_names": list(source_names),
            "sources_attempted": sources_attempted,
            "sources_succeeded": sources_succeeded,
            "sources_failed": sources_failed,
            **counters,
            "errors": errors,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": max(0.0, (finished_at - started_at).total_seconds() * 1000),
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class UnavailableCrawlAdmin:
    def status(self) -> Mapping[str, object]:
        return CrawlStatusResponse(status="disabled", enabled=False).model_dump(
            mode="json"
        )

    def schedule(self, request: CrawlScheduleRequest) -> Mapping[str, object]:
        del request
        raise AppError(
            "service_unavailable", "crawl worker 尚未初始化。", status_code=503
        )
