from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)
from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage, NptuSitePageAdapter
from nptu_assistant.crawlers.crawl_ingestion import (
    CrawlIngestionResult,
    CrawlIngestionService,
    CrawlIngestionStatus,
)
from nptu_assistant.crawlers.crawl_policy import (
    DOCUMENT_RESOURCE_SUFFIXES,
    is_crawlable_url,
)
from nptu_assistant.crawlers.http import CrawlHttpClient, CrawlHttpResponse
from nptu_assistant.crawlers.site_map import (
    SiteCrawlStatus,
    SiteMapRepository,
    SiteMapService,
)
from nptu_assistant.crawlers.site_search_cache import SingleFlightRunner
from nptu_assistant.ingestion.cleaning import content_hash

logger = logging.getLogger(__name__)


class IncrementalCrawlOutcome(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"
    BINARY = "binary"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    BLOCKED = "blocked"
    LEASE_NOT_ACQUIRED = "lease_not_acquired"
    LEASE_LOST = "lease_lost"
    INGESTION_FAILED = "ingestion_failed"


@dataclass(frozen=True, slots=True)
class IncrementalCrawlTarget:
    """Scheduler claim 出來的一個 URL 與其既有 conditional state。"""

    canonical_url: str
    allowed_hosts: tuple[str, ...] = ()
    unit: str | None = None
    depth: int = 0
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    title: str | None = None
    page_id: object | None = None
    lease_owner: str | None = None
    lease_token: object | None = None
    lease_expires_at: datetime | None = None
    ingestion_status: str | None = None
    ingestion_content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class IncrementalCrawlResult:
    target: IncrementalCrawlTarget
    outcome: IncrementalCrawlOutcome
    status_code: int | None = None
    final_url: str | None = None
    content_hash: str | None = None
    error: str | None = None
    links_discovered: int = 0
    ingestion_performed: bool = False
    retry_after: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    content_length: int | None = None

    @property
    def canonical_url(self) -> str:
        return self.target.canonical_url


@dataclass(frozen=True, slots=True)
class IncrementalCrawlRunResult:
    results: tuple[IncrementalCrawlResult, ...]

    @property
    def counts(self) -> Mapping[IncrementalCrawlOutcome, int]:
        counts = {outcome: 0 for outcome in IncrementalCrawlOutcome}
        for result in self.results:
            counts[result.outcome] += 1
        return counts


class IncrementalCrawlScheduler(Protocol):
    """B 負責的 scheduler adapter protocol；worker 不會自行啟動 scheduler。

    正式 scheduler 使用 ``owner``/``lease_duration`` claim，並以
    ``complete(claim, changed=...)`` 或 ``fail(claim, ...)`` 完成 fencing。
    """

    def claim_due(
        self,
        *,
        owner: str,
        limit: int,
        lease_duration: timedelta,
        urls: tuple[str, ...] = (),
    ) -> Sequence[object]: ...

    def complete(self, claim: object, *, changed: bool) -> bool: ...

    def fail(
        self,
        claim: object,
        *,
        http_status: int | None = None,
        retry_after: str | None = None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _ClaimedTarget:
    target: IncrementalCrawlTarget
    claim: object | None = None


class IncrementalPageSink(Protocol):
    """既有 SiteMapService 的最小 adapter，不建立第二套 ingestion pipeline。"""

    def record_fetched_page(
        self,
        page: object,
        *,
        unit: str | None,
        depth: int,
        allowed_hosts: Collection[str],
        http_status: int | None = 200,
        etag: str | None = None,
        last_modified: str | None = None,
        page_id: object | None = None,
        lease_owner: str | None = None,
        lease_token: object | None = None,
        lease_expires_at: datetime | None = None,
    ) -> object: ...

    def record_crawl_failure(
        self,
        canonical_url: str,
        *,
        http_status: int | None = None,
        status: SiteCrawlStatus = SiteCrawlStatus.FAILED,
        allow_unleased: bool = False,
    ) -> object: ...


class IncrementalCrawler:
    """有限並行、可恢復的背景 HTML crawler。

    ``run_once`` 只處理一次 scheduler claim 或明確傳入的 targets；它不建立
    task、不掛到 user request path，也不負責啟動 scheduler。
    """

    def __init__(
        self,
        http_client: CrawlHttpClient,
        page_sink: IncrementalPageSink | SiteMapService,
        *,
        scheduler: IncrementalCrawlScheduler | None = None,
        lease_runner: SingleFlightRunner | None = None,
        state_store: SiteMapRepository | None = None,
        ingestion_service: CrawlIngestionService | None = None,
        page_adapter: NptuSitePageAdapter | None = None,
        allowed_hosts: Collection[str] = ("nptu.edu.tw",),
        max_concurrency: int = 4,
        host_interval_seconds: float = 0.0,
        max_active_per_host: int = 1,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        worker_id: str = "incremental-crawler",
        lease_duration: timedelta = timedelta(minutes=5),
        db_retry_attempts: int = 3,
        db_retry_base_seconds: float = 0.1,
        db_retry_max_seconds: float = 2.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("incremental crawler concurrency 必須大於零")
        if host_interval_seconds < 0:
            raise ValueError("incremental crawler host interval 不得小於零")
        if max_active_per_host < 1:
            raise ValueError("incremental crawler 每個 host 的並行數必須大於零")
        if not worker_id.strip():
            raise ValueError("incremental crawler worker id 不得為空")
        if lease_duration <= timedelta(0):
            raise ValueError("incremental crawler lease duration 必須大於零")
        if db_retry_attempts < 1:
            raise ValueError("incremental crawler DB 重試次數必須大於零")
        if db_retry_base_seconds < 0:
            raise ValueError("incremental crawler DB 重試基礎退避不得小於零")
        if db_retry_max_seconds < db_retry_base_seconds:
            raise ValueError("incremental crawler DB 最大退避不得小於基礎退避")
        self._http = http_client
        self._page_sink = page_sink
        self._scheduler = scheduler
        self._lease_runner = lease_runner
        self._state_store = state_store
        self._ingestion = ingestion_service
        self._adapter = page_adapter or NptuSitePageAdapter()
        self._allowed_hosts = tuple(
            host.strip().lower().rstrip(".") for host in allowed_hosts if host.strip()
        )
        self._max_concurrency = max_concurrency
        self._host_interval = host_interval_seconds
        self._max_active_per_host = max_active_per_host
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._host_guard = threading.Lock()
        self._next_host_request: dict[str, float] = {}
        self._host_fetch_guard = threading.Lock()
        self._host_fetch_limits: dict[str, threading.BoundedSemaphore] = {}
        self._db_retry_attempts = db_retry_attempts
        self._db_retry_base_seconds = db_retry_base_seconds
        self._db_retry_max_seconds = db_retry_max_seconds

    def configure_runtime(
        self,
        *,
        worker_id: str | None = None,
        max_concurrency: int | None = None,
        lease_duration: timedelta | None = None,
        max_active_per_host: int | None = None,
    ) -> None:
        if worker_id is not None:
            if not worker_id.strip():
                raise ValueError("incremental crawler worker id 不得為空")
            self._worker_id = worker_id
        if max_concurrency is not None:
            if max_concurrency < 1:
                raise ValueError("incremental crawler concurrency 必須大於零")
            self._max_concurrency = max_concurrency
        if max_active_per_host is not None:
            if max_active_per_host < 1:
                raise ValueError("incremental crawler 每個 host 的並行數必須大於零")
            self._max_active_per_host = max_active_per_host
        if lease_duration is not None:
            if lease_duration <= timedelta(0):
                raise ValueError("incremental crawler lease duration 必須大於零")
            self._lease_duration = lease_duration

    def run_once(
        self,
        targets: Sequence[IncrementalCrawlTarget | Mapping[str, object] | object]
        | None = None,
        *,
        batch_size: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> IncrementalCrawlRunResult:
        if batch_size is not None and batch_size < 1:
            raise ValueError("incremental crawler batch size 必須大於零")
        started = self._clock()
        if stop_event is not None and stop_event.is_set():
            logger.info(
                "site_map_crawl_claim_skipped_after_stop",
                extra={"site_map_crawl_worker_id": self._worker_id},
            )
            return IncrementalCrawlRunResult(())
        if targets is not None:
            target_sequence = tuple(targets)
            target_batch_size = batch_size or self._max_concurrency
            if len(target_sequence) > target_batch_size:
                chunk_results: list[IncrementalCrawlResult] = []
                for offset in range(0, len(target_sequence), target_batch_size):
                    if stop_event is not None and stop_event.is_set():
                        break
                    chunk = target_sequence[offset : offset + target_batch_size]
                    chunk_results.extend(
                        self.run_once(
                            chunk,
                            batch_size=len(chunk),
                            stop_event=stop_event,
                        ).results
                    )
                return IncrementalCrawlRunResult(tuple(chunk_results))
            bounded_targets = target_sequence
            target_values = tuple(
                self._coerce_target(target) for target in bounded_targets
            )
            schedule_pages = getattr(self._scheduler, "schedule_pages", None)
            if callable(schedule_pages):
                schedule_pages(
                    urls=tuple(target.canonical_url for target in target_values),
                    run_at=self._now(),
                )
                claimed = self._claim_due(
                    limit=len(target_values),
                    urls=tuple(target.canonical_url for target in target_values),
                    stop_event=stop_event,
                )
            else:
                claimed = tuple(_ClaimedTarget(target) for target in target_values)
        else:
            claimed = self._claim_due(limit=batch_size, stop_event=stop_event)
        if not claimed:
            result = IncrementalCrawlRunResult(())
            logger.info(
                "site_map_crawl_batch_complete",
                extra={
                    "site_map_crawl_batch_count": 0,
                    "site_map_crawl_duration_ms": max(
                        0.0, (self._clock() - started) * 1000
                    ),
                    "site_map_crawl_worker_id": self._worker_id,
                },
            )
            return result

        with ThreadPoolExecutor(max_workers=self._max_concurrency) as executor:
            results = tuple(executor.map(self._crawl_claim, claimed))
        for envelope, result in zip(claimed, results, strict=True):
            self._complete_claim(envelope, result)
        result = IncrementalCrawlRunResult(results)
        counts = result.counts
        logger.info(
            "site_map_crawl_batch_complete",
            extra={
                "site_map_crawl_batch_count": len(results),
                "site_map_crawl_changed_count": counts[IncrementalCrawlOutcome.CHANGED],
                "site_map_crawl_unchanged_count": counts[
                    IncrementalCrawlOutcome.UNCHANGED
                ],
                "site_map_crawl_failed_count": sum(
                    counts[outcome]
                    for outcome in (
                        IncrementalCrawlOutcome.FAILED,
                        IncrementalCrawlOutcome.BLOCKED,
                        IncrementalCrawlOutcome.LEASE_LOST,
                        IncrementalCrawlOutcome.INGESTION_FAILED,
                    )
                ),
                "site_map_crawl_duration_ms": max(
                    0.0, (self._clock() - started) * 1000
                ),
                "site_map_crawl_worker_id": self._worker_id,
            },
        )
        return result

    def run_loop(
        self,
        *,
        poll_interval_seconds: float = 60.0,
        max_pages: int | None = None,
        max_duration_seconds: float | None = None,
        stop_event: threading.Event | None = None,
        batch_size: int | None = None,
    ) -> None:
        """Run bounded polling outside the API request path.

        The caller owns process signal handling.  This method deliberately uses
        bounded sleeps and exits cleanly when ``stop_event`` is set.
        """

        if poll_interval_seconds < 0:
            raise ValueError("crawler poll interval 不得小於零")
        if max_pages is not None and max_pages < 1:
            raise ValueError("crawler max pages 必須大於零")
        if max_duration_seconds is not None and max_duration_seconds <= 0:
            raise ValueError("crawler max duration 必須大於零")
        if batch_size is not None and batch_size < 1:
            raise ValueError("crawler batch size 必須大於零")
        started = self._clock()
        processed = 0
        while stop_event is None or not stop_event.is_set():
            if (
                max_duration_seconds is not None
                and self._clock() - started >= max_duration_seconds
            ):
                return
            remaining_pages = max_pages - processed if max_pages is not None else None
            if remaining_pages is not None and remaining_pages <= 0:
                return
            claim_limit = batch_size or self._max_concurrency
            if remaining_pages is not None:
                claim_limit = min(claim_limit, remaining_pages)
            try:
                result = self.run_once(
                    batch_size=claim_limit,
                    stop_event=stop_event,
                )
            except Exception as exc:
                if not self._is_retryable_database_error(exc):
                    raise
                logger.warning(
                    "site_map_crawl_db_retry_exhausted",
                    extra={
                        "site_map_crawl_worker_id": self._worker_id,
                        "site_map_crawl_operation": "claim",
                        "site_map_crawl_error_type": type(exc).__name__,
                    },
                    exc_info=True,
                )
                if stop_event is not None:
                    stop_event.wait(poll_interval_seconds)
                else:
                    self._sleep(poll_interval_seconds)
                continue
            processed += len(result.results)
            if max_duration_seconds is not None and (
                self._clock() - started >= max_duration_seconds
            ):
                return
            if result.results:
                if max_pages is not None and processed >= max_pages:
                    return
                continue
            if stop_event is not None:
                stop_event.wait(poll_interval_seconds)
            else:
                self._sleep(poll_interval_seconds)

    def _claim_due(
        self,
        *,
        limit: int | None = None,
        urls: tuple[str, ...] = (),
        stop_event: threading.Event | None = None,
    ) -> tuple[_ClaimedTarget, ...]:
        if self._scheduler is None:
            return ()
        if stop_event is not None and stop_event.is_set():
            return ()
        scheduler = self._scheduler
        claim_limit = limit or self._max_concurrency
        try:
            try:
                claims = cast(
                    Sequence[object],
                    self._with_db_retry(
                        lambda: scheduler.claim_due(
                            owner=self._worker_id,
                            limit=claim_limit,
                            lease_duration=self._lease_duration,
                            urls=urls,
                        ),
                        operation_name="claim",
                    ),
                )
            except TypeError:
                claims = cast(
                    Sequence[object],
                    self._with_db_retry(
                        lambda: scheduler.claim_due(
                            owner=self._worker_id,
                            limit=claim_limit,
                            lease_duration=self._lease_duration,
                        ),
                        operation_name="claim",
                    ),
                )
            return tuple(
                _ClaimedTarget(self._coerce_target(claim), claim) for claim in claims
            )
        except TypeError:
            # 供尚未切換到正式 lease adapter 的測試 double 使用；正式
            # scheduler 不會走這條路徑。
            legacy_claim_due = cast(
                Callable[..., Sequence[object]], scheduler.claim_due
            )
            targets = legacy_claim_due(limit=claim_limit, now=self._now())
            return tuple(
                _ClaimedTarget(self._coerce_target(target)) for target in targets
            )

    def _crawl_claim(self, envelope: _ClaimedTarget) -> IncrementalCrawlResult:
        return self._crawl_one(envelope.target, claim=envelope.claim)

    def _complete_claim(
        self,
        envelope: _ClaimedTarget,
        result: IncrementalCrawlResult,
    ) -> None:
        if self._scheduler is None:
            return
        if envelope.claim is not None:
            if not self._renew_claim(envelope.claim):
                logger.info(
                    "site_map_crawl_completion_skipped_after_lease_loss",
                    extra={"site_map_crawl_worker_id": self._worker_id},
                )
                return
            if result.outcome in {
                IncrementalCrawlOutcome.CHANGED,
                IncrementalCrawlOutcome.UNCHANGED,
            }:
                complete = getattr(self._scheduler, "complete", None)
                if callable(complete):
                    try:
                        completed = self._with_db_retry(
                            lambda: complete(
                                envelope.claim,
                                changed=result.outcome
                                is IncrementalCrawlOutcome.CHANGED,
                                http_status=result.status_code,
                                links_discovered=result.links_discovered,
                                ingestion_performed=result.ingestion_performed,
                                etag=result.etag,
                                last_modified=result.last_modified,
                                content_type=result.content_type,
                                content_length=result.content_length,
                                final_url=result.final_url,
                            ),
                            operation_name="complete",
                        )
                    except TypeError:
                        complete(
                            envelope.claim,
                            changed=result.outcome is IncrementalCrawlOutcome.CHANGED,
                        )
                        completed = True
                    if completed is False:
                        logger.info(
                            "site_map_crawl_completion_rejected",
                            extra={
                                "site_map_crawl_worker_id": self._worker_id,
                                "site_map_crawl_outcome": result.outcome.value,
                            },
                        )
            else:
                fail = getattr(self._scheduler, "fail", None)
                if callable(fail):
                    status_code = result.status_code
                    if result.outcome in {
                        IncrementalCrawlOutcome.BINARY,
                        IncrementalCrawlOutcome.UNSUPPORTED,
                    } and status_code in {None, 200}:
                        status_code = 415
                    try:
                        failed = self._with_db_retry(
                            lambda: fail(
                                envelope.claim,
                                http_status=status_code,
                                error_kind=result.outcome.value,
                                error_message=result.error,
                                retry_after=result.retry_after,
                                content_type=result.content_type,
                                content_length=result.content_length,
                                final_url=result.final_url,
                                ingestion_performed=result.ingestion_performed,
                            ),
                            operation_name="fail",
                        )
                    except TypeError:
                        fail(envelope.claim, http_status=status_code)
                        failed = True
                    if failed is False:
                        logger.info(
                            "site_map_crawl_failure_rejected",
                            extra={
                                "site_map_crawl_worker_id": self._worker_id,
                                "site_map_crawl_outcome": result.outcome.value,
                            },
                        )
            return
        complete = getattr(self._scheduler, "complete", None)
        if callable(complete):
            complete(result)

    def _crawl_one(
        self,
        target: IncrementalCrawlTarget,
        *,
        claim: object | None = None,
    ) -> IncrementalCrawlResult:
        if not is_allowed_nptu_url(target.canonical_url):
            return self._result(
                target, IncrementalCrawlOutcome.BLOCKED, error="URL 不在 NPTU allowlist"
            )
        allowed_hosts = target.allowed_hosts or self._allowed_hosts
        if not is_allowed_source_url(target.canonical_url, allowed_hosts):
            return self._result(
                target,
                IncrementalCrawlOutcome.BLOCKED,
                error="URL 不在來源 host allowlist",
            )

        if not is_crawlable_url(target.canonical_url):
            outcome = (
                IncrementalCrawlOutcome.BINARY
                if urlsplit(target.canonical_url)
                .path.casefold()
                .endswith(tuple(DOCUMENT_RESOURCE_SUFFIXES))
                else IncrementalCrawlOutcome.UNSUPPORTED
            )
            self._mark_excluded(target.canonical_url, claim=claim)
            return self._result(target, outcome, error="非 HTML 可解析資源")

        lease = None
        try:
            if self._lease_runner is not None:
                lease = self._lease_runner.acquire(
                    f"incremental-crawler:{target.canonical_url}"
                )
                if lease is None:
                    return self._result(
                        target,
                        IncrementalCrawlOutcome.LEASE_NOT_ACQUIRED,
                        error="URL 已由其他 crawler worker 處理",
                    )

            if claim is not None and not self._renew_claim(claim):
                return self._result(
                    target,
                    IncrementalCrawlOutcome.LEASE_LOST,
                    error="page lease 在抓取前失效，拒絕開始耗時工作",
                )

            self._throttle_host(target.canonical_url)
            ingestion_pending = self._ingestion_pending(target, claim=claim)
            headers = {} if ingestion_pending else self._conditional_headers(target)
            response = self._fetch_response(
                target.canonical_url,
                allowed_hosts=allowed_hosts,
                request_headers=headers or None,
            )
            self._validate_final_url(response.url, allowed_hosts)
            if response.status_code == 304:
                if claim is None:
                    self._record_not_modified(target, response)
                return self._result(
                    target,
                    IncrementalCrawlOutcome.UNCHANGED,
                    status_code=304,
                    final_url=response.url,
                    content_hash=target.content_hash,
                    retry_after=response.headers.get("retry-after"),
                    etag=response.headers.get("etag") or target.etag,
                    last_modified=response.headers.get("last-modified")
                    or target.last_modified,
                    content_type=response.content_type,
                    content_length=len(response.content),
                )
            if response.status_code != 200:
                if claim is None:
                    self._record_failure(target.canonical_url, response.status_code)
                return self._result(
                    target,
                    IncrementalCrawlOutcome.FAILED,
                    status_code=response.status_code,
                    final_url=response.url,
                    retry_after=response.headers.get("retry-after"),
                    error=f"HTTP status {response.status_code}",
                    content_type=response.content_type,
                    content_length=len(response.content),
                )
            return self._handle_html_response(
                target,
                response,
                allowed_hosts,
                claim=claim,
                ingestion_pending=ingestion_pending,
            )
        except Exception as exc:
            logger.exception(
                "incremental crawl failed",
                extra={"url": target.canonical_url},
            )
            status_code = self._status_code(exc)
            if claim is None:
                self._record_failure(target.canonical_url, status_code)
            if claim is not None and self._is_lease_error(exc):
                return self._result(
                    target,
                    IncrementalCrawlOutcome.LEASE_LOST,
                    status_code=status_code,
                    error=str(exc),
                )
            return self._result(
                target,
                IncrementalCrawlOutcome.FAILED,
                status_code=status_code,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if lease is not None:
                lease.release()

    def _handle_html_response(
        self,
        target: IncrementalCrawlTarget,
        response: CrawlHttpResponse,
        allowed_hosts: Collection[str],
        *,
        claim: object | None = None,
        ingestion_pending: bool = False,
    ) -> IncrementalCrawlResult:
        media_type = response.content_type.split(";", 1)[0].strip()
        if media_type not in {"text/html", "application/xhtml+xml"}:
            outcome = (
                IncrementalCrawlOutcome.BINARY
                if self._is_binary_response(target.canonical_url, media_type)
                else IncrementalCrawlOutcome.UNSUPPORTED
            )
            self._mark_excluded(
                target.canonical_url,
                response.status_code,
                claim=claim,
            )
            return self._result(
                target,
                outcome,
                status_code=response.status_code,
                final_url=response.url,
                error=f"不支援的 content-type：{media_type or '未提供'}",
                content_type=response.content_type,
                content_length=len(response.content),
            )

        if claim is not None and not self._renew_claim(claim):
            return self._result(
                target,
                IncrementalCrawlOutcome.LEASE_LOST,
                status_code=response.status_code,
                final_url=response.url,
                error="page lease 在解析前失效，拒絕開始耗時工作",
            )
        page = self._adapter.parse_page(
            response.text,
            response.url,
            allowed_hosts=tuple(allowed_hosts),
        )
        digest = content_hash(page.body)
        if claim is not None and not self._renew_claim(claim):
            return self._result(
                target,
                IncrementalCrawlOutcome.LEASE_LOST,
                status_code=response.status_code,
                final_url=response.url,
                error="page lease 已失效，拒絕寫入結果",
            )
        kwargs = {
            "unit": target.unit,
            "depth": target.depth,
            "allowed_hosts": allowed_hosts,
            "http_status": response.status_code,
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
        }
        if claim is not None:
            kwargs.update(
                page_id=getattr(claim, "page_id", None),
                lease_owner=getattr(claim, "owner", None),
                lease_token=getattr(claim, "token", None),
                lease_expires_at=getattr(claim, "lease_expires_at", None),
            )
        try:
            self._page_sink.record_fetched_page(page, **kwargs)
        except RuntimeError as exc:
            if "lease" in str(exc).casefold():
                return self._result(
                    target,
                    IncrementalCrawlOutcome.LEASE_LOST,
                    status_code=response.status_code,
                    final_url=response.url,
                    error=str(exc),
                )
            raise
        outcome = (
            IncrementalCrawlOutcome.UNCHANGED
            if target.content_hash is not None and target.content_hash == digest
            else IncrementalCrawlOutcome.CHANGED
        )
        ingestion_performed = False
        should_ingest = self._ingestion is not None and (
            outcome is IncrementalCrawlOutcome.CHANGED or ingestion_pending
        )
        if should_ingest:
            if claim is not None and not self._renew_claim(claim):
                return self._result(
                    target,
                    IncrementalCrawlOutcome.LEASE_LOST,
                    status_code=response.status_code,
                    final_url=response.url,
                    content_hash=digest,
                    error="page lease 在 ingestion 前失效，拒絕開始耗時工作",
                    links_discovered=len(page.links),
                )
            ingestion_result = self._ingest_page(
                page,
                target=target,
                claim=claim,
            )
            if ingestion_result.status in {
                CrawlIngestionStatus.FAILED,
                CrawlIngestionStatus.PARTIAL,
            }:
                return self._result(
                    target,
                    IncrementalCrawlOutcome.INGESTION_FAILED,
                    status_code=response.status_code,
                    final_url=response.url,
                    content_hash=digest,
                    error=ingestion_result.error,
                    links_discovered=len(page.links),
                    retry_after=response.headers.get("retry-after"),
                )
            ingestion_performed = (
                ingestion_result.status is CrawlIngestionStatus.CREATED
            )
        return self._result(
            target,
            outcome,
            status_code=response.status_code,
            final_url=response.url,
            content_hash=digest,
            links_discovered=len(page.links),
            ingestion_performed=ingestion_performed,
            retry_after=response.headers.get("retry-after"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            content_type=response.content_type,
            content_length=len(response.content),
        )

    def _renew_claim(self, claim: object) -> bool:
        if self._scheduler is None:
            return True
        renew = getattr(self._scheduler, "renew", None)
        if not callable(renew):
            return True
        try:
            return bool(
                self._with_db_retry(
                    lambda: renew(claim, lease_duration=self._lease_duration),
                    operation_name="renew",
                )
            )
        except TypeError:
            return True

    def _ingestion_pending(
        self,
        target: IncrementalCrawlTarget,
        *,
        claim: object | None,
    ) -> bool:
        if self._ingestion is None or target.content_hash is None:
            return False
        if (
            target.ingestion_status in {"pending", "failed"}
            and target.ingestion_content_hash == target.content_hash
        ):
            return True
        checker = getattr(self._ingestion, "needs_ingestion", None)
        if not callable(checker):
            return False
        context = self._ingestion_context(target, claim)
        try:
            return bool(checker(target.canonical_url, target.content_hash, **context))
        except TypeError as exc:
            if "unexpected keyword" not in str(exc).casefold():
                raise
            return bool(checker(target.canonical_url, target.content_hash))

    def _ingest_page(
        self,
        page: NptuSitePage,
        *,
        target: IncrementalCrawlTarget,
        claim: object | None,
    ) -> CrawlIngestionResult:
        assert self._ingestion is not None
        context = self._ingestion_context(target, claim)
        try:
            return self._ingestion.ingest_page(
                page,
                unit=target.unit,
                **cast(dict[str, Any], context),
            )
        except TypeError as exc:
            # Keep existing test doubles and third-party adapters compatible.
            if context and "unexpected keyword" not in str(exc).casefold():
                raise
            return self._ingestion.ingest_page(page, unit=target.unit)

    @staticmethod
    def _ingestion_context(
        target: IncrementalCrawlTarget,
        claim: object | None,
    ) -> dict[str, object]:
        source = claim if claim is not None else target

        def get(name: str, fallback: object = None) -> object:
            value = getattr(source, name, fallback)
            return fallback if value is None else value

        page_id = get("page_id")
        owner = get("owner", get("lease_owner"))
        token = get("token", get("lease_token"))
        expires_at = get("lease_expires_at")
        if page_id is None or owner is None or token is None:
            return {}
        return {
            "page_id": page_id,
            "lease_owner": owner,
            "lease_token": token,
            "lease_expires_at": expires_at,
        }

    def _fetch_response(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str],
        request_headers: Mapping[str, str] | None,
    ) -> CrawlHttpResponse:
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
        with self._host_fetch_guard:
            semaphore = self._host_fetch_limits.get(host)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(self._max_active_per_host)
                self._host_fetch_limits[host] = semaphore
        semaphore.acquire()
        try:
            try:
                return self._http.get_response(
                    url,
                    allowed_hosts=allowed_hosts,
                    request_headers=request_headers,
                    preserve_error_status=True,
                )
            except TypeError:
                return self._http.get_response(
                    url,
                    allowed_hosts=allowed_hosts,
                    request_headers=request_headers,
                )
        finally:
            semaphore.release()

    def _with_db_retry(
        self,
        action: Callable[[], object],
        *,
        operation_name: str,
    ) -> object:
        delay = self._db_retry_base_seconds
        for attempt in range(1, self._db_retry_attempts + 1):
            try:
                return action()
            except Exception as exc:
                if (
                    not self._is_retryable_database_error(exc)
                    or attempt >= self._db_retry_attempts
                ):
                    raise
                backoff = min(delay, self._db_retry_max_seconds)
                logger.warning(
                    "site_map_crawl_db_retry",
                    extra={
                        "site_map_crawl_operation": operation_name,
                        "site_map_crawl_attempt": attempt,
                        "site_map_crawl_max_attempts": self._db_retry_attempts,
                        "site_map_crawl_backoff_seconds": backoff,
                        "site_map_crawl_error_type": type(exc).__name__,
                        "site_map_crawl_worker_id": self._worker_id,
                    },
                )
                if backoff > 0:
                    self._sleep(backoff)
                delay = min(
                    self._db_retry_max_seconds,
                    delay * 2 if delay > 0 else self._db_retry_max_seconds,
                )
        raise AssertionError("unreachable")

    @staticmethod
    def _is_retryable_database_error(exc: Exception) -> bool:
        return isinstance(exc, (OperationalError, InterfaceError, TimeoutError)) or (
            isinstance(exc, DBAPIError) and exc.connection_invalidated
        )

    def _record_not_modified(
        self, target: IncrementalCrawlTarget, response: CrawlHttpResponse
    ) -> None:
        if self._state_store is None or target.content_hash is None:
            return
        self._state_store.record_crawl_success(
            response.url,
            title=target.title,
            content_hash=target.content_hash,
            http_status=304,
            etag=response.headers.get("etag") or target.etag,
            last_modified=response.headers.get("last-modified") or target.last_modified,
            allow_unleased=True,
        )

    @staticmethod
    def _is_lease_error(exc: Exception) -> bool:
        return "lease" in str(exc).casefold()

    def _record_failure(self, url: str, status_code: int | None) -> None:
        try:
            self._page_sink.record_crawl_failure(
                url,
                http_status=status_code,
                allow_unleased=True,
            )
        except Exception:
            logger.debug(
                "crawl failure state write failed",
                extra={"url": url},
                exc_info=True,
            )

    def _mark_excluded(
        self,
        url: str,
        status_code: int | None = None,
        *,
        claim: object | None = None,
    ) -> None:
        if claim is not None:
            return
        try:
            self._page_sink.record_crawl_failure(
                url,
                http_status=status_code,
                status=SiteCrawlStatus.EXCLUDED,
                allow_unleased=True,
            )
        except Exception:
            logger.debug(
                "crawl exclusion state write failed",
                extra={"url": url},
                exc_info=True,
            )

    def _throttle_host(self, url: str) -> None:
        if self._host_interval == 0:
            return
        host = (urlsplit(url).hostname or "").casefold().rstrip(".")
        with self._host_guard:
            now = self._clock()
            next_allowed = self._next_host_request.get(host, now)
            delay = max(0.0, next_allowed - now)
            self._next_host_request[host] = max(now, next_allowed) + self._host_interval
        if delay:
            self._sleep(delay)

    @staticmethod
    def _conditional_headers(target: IncrementalCrawlTarget) -> dict[str, str]:
        headers: dict[str, str] = {}
        if target.etag:
            headers["If-None-Match"] = target.etag
        if target.last_modified:
            headers["If-Modified-Since"] = target.last_modified
        return headers

    @staticmethod
    def _validate_final_url(url: str, allowed_hosts: Collection[str]) -> None:
        if not is_allowed_nptu_url(url) or not is_allowed_source_url(
            url, allowed_hosts
        ):
            raise ValueError("redirect final URL 不在來源 host allowlist")

    @staticmethod
    def _is_binary_response(url: str, media_type: str) -> bool:
        return (
            media_type.startswith(("image/", "audio/", "video/"))
            or media_type
            in {
                "application/octet-stream",
                "application/pdf",
                "application/msword",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.ms-excel",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
            or urlsplit(url).path.casefold().endswith(tuple(DOCUMENT_RESOURCE_SUFFIXES))
        )

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code if isinstance(status_code, int) else None

    @staticmethod
    def _result(
        target: IncrementalCrawlTarget,
        outcome: IncrementalCrawlOutcome,
        *,
        status_code: int | None = None,
        final_url: str | None = None,
        content_hash: str | None = None,
        error: str | None = None,
        links_discovered: int = 0,
        ingestion_performed: bool = False,
        retry_after: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        content_type: str | None = None,
        content_length: int | None = None,
    ) -> IncrementalCrawlResult:
        return IncrementalCrawlResult(
            target=target,
            outcome=outcome,
            status_code=status_code,
            final_url=final_url,
            content_hash=content_hash,
            error=error,
            links_discovered=links_discovered,
            ingestion_performed=ingestion_performed,
            retry_after=retry_after,
            etag=etag,
            last_modified=last_modified,
            content_type=content_type,
            content_length=content_length,
        )

    def _coerce_target(self, value: object) -> IncrementalCrawlTarget:
        if isinstance(value, IncrementalCrawlTarget):
            return value
        if isinstance(value, str):
            return IncrementalCrawlTarget(value, self._allowed_hosts)

        def get(name: str, default: object = None) -> object:
            if isinstance(value, Mapping):
                return value.get(name, default)
            return getattr(value, name, default)

        raw_url = get("canonical_url", get("url"))
        if not isinstance(raw_url, str) or not raw_url:
            raise TypeError("scheduler target 必須提供 canonical_url")
        try:
            normalized_url = canonicalize_nptu_url(raw_url)
        except ValueError:
            normalized_url = raw_url
        raw_hosts = get("allowed_hosts", self._allowed_hosts)
        hosts = (
            tuple(item for item in raw_hosts if isinstance(item, str))
            if isinstance(raw_hosts, Collection) and not isinstance(raw_hosts, str)
            else self._allowed_hosts
        )
        raw_depth = get("depth", get("minimum_depth", 0))
        depth = int(raw_depth) if isinstance(raw_depth, (int, float, str)) else 0
        return IncrementalCrawlTarget(
            canonical_url=normalized_url,
            allowed_hosts=hosts,
            unit=cast(str | None, get("unit")),
            depth=depth,
            etag=cast(str | None, get("etag")),
            last_modified=cast(str | None, get("last_modified")),
            content_hash=cast(str | None, get("content_hash")),
            title=cast(str | None, get("title")),
            page_id=get("page_id"),
            lease_owner=cast(str | None, get("owner", get("lease_owner"))),
            lease_token=get("token", get("lease_token")),
            lease_expires_at=cast(datetime | None, get("lease_expires_at")),
            ingestion_status=cast(str | None, get("ingestion_status")),
            ingestion_content_hash=cast(str | None, get("ingestion_content_hash")),
        )


IncrementalCrawlerWorker = IncrementalCrawler
