from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from time import perf_counter
from urllib.parse import urlsplit

import pytest
from nptu_assistant.crawlers.site_map import (
    FrontierPolicy,
    SiteCrawlStatus,
    SiteDiscoverySource,
    SiteLinkType,
    SiteLinkUpsert,
    SitePageType,
    SitePageUpsert,
)
from nptu_assistant.db.models import SiteLink, SitePage
from nptu_assistant.db.site_map import SqlSiteMapRepository
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要已完成 migration 的 real PostgreSQL acceptance database",
)


@dataclass(frozen=True, slots=True)
class TracedStatement:
    sql: str
    parameters: object


@dataclass(slots=True)
class SqlTrace:
    statements: list[TracedStatement] = field(default_factory=list)
    begins: int = 0
    commits: int = 0
    rollbacks: int = 0
    elapsed_ms: float = 0.0

    @property
    def statement_count(self) -> int:
        return len(self.statements)

    @property
    def transaction_count(self) -> int:
        return self.begins

    @property
    def advisory_lock_hosts(self) -> tuple[str, ...]:
        hosts: list[str] = []
        for item in self.statements:
            normalized = " ".join(item.sql.lower().split())
            if (
                "pg_advisory_xact_lock(hashtext" not in normalized
                and "pg_advisory_xact_lock(lock_key)" not in normalized
            ):
                continue
            if isinstance(item.parameters, Mapping):
                host = item.parameters.get("host")
                if isinstance(host, str):
                    hosts.append(host)
                requested_hosts = item.parameters.get("hosts")
                if isinstance(requested_hosts, (list, tuple)):
                    hosts.extend(str(value) for value in requested_hosts)
        return tuple(hosts)

    def sql_shapes(self) -> tuple[str, ...]:
        return tuple(" ".join(item.sql.split()) for item in self.statements)

    def summary(self) -> str:
        return (
            f"transactions={self.transaction_count} "
            f"statements={self.statement_count} "
            f"commits={self.commits} rollbacks={self.rollbacks} "
            f"elapsed_ms={self.elapsed_ms:.2f} "
            f"lock_hosts={self.advisory_lock_hosts!r}"
        )


class SqlTracer:
    """Capture only the current thread's PostgreSQL calls on one Engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.local = threading.local()
        event.listen(engine, "before_cursor_execute", self._before_cursor_execute)
        event.listen(engine, "begin", self._begin)
        event.listen(engine, "commit", self._commit)
        event.listen(engine, "rollback", self._rollback)

    def close(self) -> None:
        event.remove(self.engine, "before_cursor_execute", self._before_cursor_execute)
        event.remove(self.engine, "begin", self._begin)
        event.remove(self.engine, "commit", self._commit)
        event.remove(self.engine, "rollback", self._rollback)

    @contextmanager
    def capture(self) -> Iterator[SqlTrace]:
        trace = SqlTrace()
        previous = getattr(self.local, "trace", None)
        self.local.trace = trace
        started = perf_counter()
        try:
            yield trace
        finally:
            trace.elapsed_ms = (perf_counter() - started) * 1000
            self.local.trace = previous

    def _trace(self) -> SqlTrace | None:
        return getattr(self.local, "trace", None)

    def _before_cursor_execute(
        self,
        _conn: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        trace = self._trace()
        if trace is not None and statement.strip():
            trace.statements.append(TracedStatement(statement, parameters))

    def _begin(self, _conn: object) -> None:
        trace = self._trace()
        if trace is not None:
            trace.begins += 1

    def _commit(self, _conn: object) -> None:
        trace = self._trace()
        if trace is not None:
            trace.commits += 1

    def _rollback(self, _conn: object) -> None:
        trace = self._trace()
        if trace is not None:
            trace.rollbacks += 1


def make_factory() -> tuple[sessionmaker[Session], Engine]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def cleanup(factory: sessionmaker[Session], token: str) -> None:
    with factory.begin() as session:
        session.execute(
            delete(SitePage).where(SitePage.canonical_url.like(f"%{token}%"))
        )


def page(url: str, *, title: str | None = None) -> SitePageUpsert:
    return SitePageUpsert(
        canonical_url=url,
        title=title,
        page_type=SitePageType.GENERAL_PAGE,
        discovery_source=SiteDiscoverySource.INTERNAL_LINK,
    )


def link(url: str, *, anchor: str | None = None) -> SiteLinkUpsert:
    return SiteLinkUpsert(
        target=page(url),
        anchor_text=anchor or url.rsplit("/", 1)[-1],
        link_type=SiteLinkType.CONTENT,
    )


def seed_page(
    factory: sessionmaker[Session],
    url: str,
    *,
    status: SiteCrawlStatus = SiteCrawlStatus.SUCCESS,
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    parsed = urlsplit(url)
    with factory.begin() as session:
        session.add(
            SitePage(
                canonical_url=url,
                host=parsed.hostname or "",
                path=parsed.path or "/",
                page_type=SitePageType.GENERAL_PAGE.value,
                discovery_source=SiteDiscoverySource.INTERNAL_LINK.value,
                crawl_status=status.value,
                next_crawl_at=current,
                last_discovered_at=current,
                is_active=True,
                is_indexable=True,
            )
        )


def persist(
    repository: SqlSiteMapRepository,
    source_url: str,
    links: Sequence[SiteLinkUpsert],
    *,
    title: str | None = "來源",
    content_hash: str = "a" * 64,
) -> object:
    return repository.persist_fetched_page(
        page(source_url),
        title=title,
        content_hash=content_hash,
        http_status=200,
        links=links,
        allow_unleased=True,
    )


def count_rows(factory: sessionmaker[Session], token: str) -> tuple[int, int]:
    with factory() as session:
        page_ids = select(SitePage.id).where(SitePage.canonical_url.like(f"%{token}%"))
        pages = int(
            session.scalar(
                select(func.count())
                .select_from(SitePage)
                .where(SitePage.canonical_url.like(f"%{token}%"))
            )
            or 0
        )
        edges = int(
            session.scalar(
                select(func.count())
                .select_from(SiteLink)
                .where(
                    SiteLink.source_page_id.in_(page_ids),
                    SiteLink.target_page_id.in_(page_ids),
                )
            )
            or 0
        )
    return pages, edges


def pending_count(factory: sessionmaker[Session], token: str) -> dict[str, int]:
    with factory() as session:
        rows = session.execute(
            select(SitePage.host, func.count())
            .where(
                SitePage.canonical_url.like(f"%{token}%"),
                SitePage.is_active.is_(True),
                SitePage.is_indexable.is_(True),
                SitePage.crawl_status.in_(
                    [
                        SiteCrawlStatus.DISCOVERED.value,
                        SiteCrawlStatus.QUEUED.value,
                        SiteCrawlStatus.FAILED.value,
                        SiteCrawlStatus.FETCHING.value,
                    ]
                ),
            )
            .group_by(SitePage.host)
        ).all()
    return {host: int(count) for host, count in rows}


def existing_pending_count(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(SitePage)
                .where(
                    SitePage.is_active.is_(True),
                    SitePage.is_indexable.is_(True),
                    SitePage.crawl_status.in_(
                        [
                            SiteCrawlStatus.DISCOVERED.value,
                            SiteCrawlStatus.QUEUED.value,
                            SiteCrawlStatus.FAILED.value,
                            SiteCrawlStatus.FETCHING.value,
                        ]
                    ),
                )
            )
            or 0
        )


def test_frontier_statement_count_does_not_scale_with_link_count() -> None:
    factory, engine = make_factory()
    token = f"p311-link-scale-{uuid.uuid4().hex}"
    host = f"{token}.nptu.edu.tw"
    policy = FrontierPolicy(
        per_page_link_cap=200,
        per_host_pending_cap=200,
        max_pending_total=10_000,
        new_target_delay=timedelta(0),
    )
    repository = SqlSiteMapRepository(factory, frontier_policy=policy)
    tracer = SqlTracer(engine)
    traces: dict[int, SqlTrace] = {}
    try:
        for link_count in (1, 10, 100):
            source_url = f"https://{host}/source-{link_count}"
            links = tuple(
                link(f"https://{host}/target-{link_count}-{index}")
                for index in range(link_count)
            )
            with tracer.capture() as trace:
                result = persist(repository, source_url, links)
            traces[link_count] = trace
            assert result.links_created == link_count, trace.summary()
            assert trace.transaction_count == 1, trace.summary()
            assert trace.commits == 1, trace.summary()
            assert trace.rollbacks == 0, trace.summary()

        statement_counts = [traces[count].statement_count for count in (1, 10, 100)]
        assert max(statement_counts) - min(statement_counts) <= 3, (
            "frontier persistence statement count must be bounded by workload shape; "
            + "; ".join(
                f"links={count} {traces[count].summary()}" for count in (1, 10, 100)
            )
        )
        pages, edges = count_rows(factory, token)
        assert pages == 111 + 3
        assert edges == 111
        print(
            "frontier_persistence_link_scale "
            + "; ".join(
                f"links={count} {traces[count].summary()}" for count in (1, 10, 100)
            )
            + f" rows_pages={pages} rows_edges={edges}"
        )
    finally:
        tracer.close()
        cleanup(factory, token)
        engine.dispose()


@pytest.mark.parametrize("host_count", (1, 10, 100))
def test_frontier_statement_count_is_bounded_by_host_count(
    host_count: int,
) -> None:
    factory, engine = make_factory()
    token = f"p311-host-scale-{host_count}-{uuid.uuid4().hex}"
    source_host = f"{token}-source.nptu.edu.tw"
    policy = FrontierPolicy(
        per_page_link_cap=200,
        per_host_pending_cap=200,
        max_pending_total=10_000,
        new_target_delay=timedelta(0),
    )
    repository = SqlSiteMapRepository(factory, frontier_policy=policy)
    tracer = SqlTracer(engine)
    links = tuple(
        link(f"https://{token}-{index % host_count}.nptu.edu.tw/target-{index}")
        for index in range(100)
    )
    source_url = f"https://{source_host}/source"
    try:
        with tracer.capture() as trace:
            result = persist(repository, source_url, links)
        assert result.links_created == 100, trace.summary()
        assert result.target_created == 100, trace.summary()
        assert trace.transaction_count == 1, trace.summary()
        assert trace.commits == 1, trace.summary()
        assert trace.rollbacks == 0, trace.summary()
        assert trace.advisory_lock_hosts == tuple(sorted(trace.advisory_lock_hosts)), (
            "advisory lock host order must be deterministic and sorted; "
            + trace.summary()
        )
        if host_count == 100:
            assert trace.statement_count <= 9, (
                "100 hosts must stay within the <=9 SQL acceptance budget; "
                + trace.summary()
            )
        print(
            f"frontier_persistence_host_scale hosts={host_count} "
            f"{trace.summary()} rows={count_rows(factory, token)}"
        )
    finally:
        tracer.close()
        cleanup(factory, token)
        engine.dispose()


def test_frontier_persistence_deduplicates_existing_new_and_non_html_targets() -> None:
    factory, engine = make_factory()
    token = f"p311-target-shapes-{uuid.uuid4().hex}"
    host = f"{token}.nptu.edu.tw"
    source_url = f"https://{host}/source"
    existing_url = f"https://{host}/existing"
    new_url = f"https://{host}/new"
    document_url = f"https://{host}/download/notice.pdf"
    seed_page(factory, existing_url)
    repository = SqlSiteMapRepository(
        factory,
        frontier_policy=FrontierPolicy(
            per_page_link_cap=20,
            per_host_pending_cap=20,
            max_pending_total=10_000,
            new_target_delay=timedelta(minutes=3),
        ),
    )
    try:
        result = persist(
            repository,
            source_url,
            (
                link(existing_url, anchor="既有"),
                link(existing_url + "#fragment", anchor="重複既有"),
                link(new_url, anchor="新增"),
                link(new_url + "#fragment", anchor="重複新增"),
                link(document_url, anchor="文件"),
            ),
        )
        assert result.target_created == 2
        assert result.links_created == 3
        pages, edges = count_rows(factory, token)
        assert pages == 4
        assert edges == 3
        with factory() as session:
            stored = session.scalars(
                select(SitePage).where(
                    SitePage.canonical_url.in_((existing_url, new_url, document_url))
                )
            ).all()
            by_url = {item.canonical_url: item for item in stored}
            assert set(by_url) == {existing_url, new_url, document_url}
            assert by_url[new_url].is_indexable is True
            assert by_url[new_url].crawl_status == SiteCrawlStatus.DISCOVERED.value
            assert by_url[document_url].is_indexable is False
            assert by_url[document_url].crawl_status == SiteCrawlStatus.EXCLUDED.value
    finally:
        cleanup(factory, token)
        engine.dispose()


def test_frontier_persistence_enforces_global_and_per_host_pending_caps() -> None:
    factory, engine = make_factory()
    token = f"p311-cap-{uuid.uuid4().hex}"
    source_host = f"{token}-source.nptu.edu.tw"
    host_a = f"{token}-a.nptu.edu.tw"
    host_b = f"{token}-b.nptu.edu.tw"
    baseline = existing_pending_count(factory)
    policy = FrontierPolicy(
        per_page_link_cap=20,
        per_host_pending_cap=1,
        max_pending_total=baseline + 2,
        new_target_delay=timedelta(0),
    )
    repository = SqlSiteMapRepository(factory, frontier_policy=policy)
    try:
        result = persist(
            repository,
            f"https://{source_host}/source",
            (
                link(f"https://{host_a}/target-1"),
                link(f"https://{host_a}/target-2"),
                link(f"https://{host_b}/target-1"),
                link(f"https://{host_b}/target-2"),
            ),
        )
        assert result.frontier_skipped == 2
        assert result.target_created == 2
        assert result.links_created == 2
        by_host = pending_count(factory, token)
        assert by_host == {host_a: 1, host_b: 1}
        assert sum(by_host.values()) == 2
    finally:
        cleanup(factory, token)
        engine.dispose()


def test_two_concurrent_frontier_writers_do_not_deadlock_and_are_idempotent() -> None:
    factory, engine = make_factory()
    token = f"p311-concurrent-{uuid.uuid4().hex}"
    hosts = tuple(f"{token}-{index}.nptu.edu.tw" for index in range(4))
    repository = SqlSiteMapRepository(
        factory,
        frontier_policy=FrontierPolicy(
            per_page_link_cap=20,
            per_host_pending_cap=20,
            max_pending_total=10_000,
            new_target_delay=timedelta(0),
        ),
    )
    tracer = SqlTracer(engine)

    def write(writer: int) -> tuple[object, SqlTrace]:
        ordered_hosts = hosts if writer == 0 else tuple(reversed(hosts))
        links = tuple(link(f"https://{host}/writer-{writer}") for host in ordered_hosts)
        with tracer.capture() as trace:
            result = persist(
                repository,
                f"https://{token}-source-{writer}.nptu.edu.tw/source",
                links,
            )
        return result, trace

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [executor.submit(write, writer) for writer in (0, 1)]
        results = [future.result(timeout=30) for future in futures]
        for result, trace in results:
            assert result.links_created == 4, trace.summary()
            assert trace.transaction_count == 1, trace.summary()
            assert trace.commits == 1, trace.summary()
            assert trace.rollbacks == 0, trace.summary()
            assert trace.advisory_lock_hosts == tuple(sorted(hosts)), trace.summary()
        assert count_rows(factory, token) == (10, 8)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        tracer.close()
        cleanup(factory, token)
        engine.dispose()


def test_frontier_persistence_rolls_back_source_targets_and_edges_together() -> None:
    factory, engine = make_factory()
    token = f"p311-rollback-{uuid.uuid4().hex}"
    source_url = f"https://{token}.nptu.edu.tw/source"
    target_url = f"https://{token}.nptu.edu.tw/target"
    repository = SqlSiteMapRepository(
        factory,
        frontier_policy=FrontierPolicy(
            per_page_link_cap=20,
            per_host_pending_cap=20,
            max_pending_total=10_000,
        ),
    )
    tracer = SqlTracer(engine)
    try:
        with tracer.capture() as trace, pytest.raises(DataError):
            persist(
                repository,
                source_url,
                (link(target_url),),
                title="x" * 501,
            )
        assert trace.transaction_count == 1, trace.summary()
        assert trace.commits == 0, trace.summary()
        assert trace.rollbacks >= 1, trace.summary()
        assert count_rows(factory, token) == (0, 0), trace.summary()
    finally:
        tracer.close()
        cleanup(factory, token)
        engine.dispose()
