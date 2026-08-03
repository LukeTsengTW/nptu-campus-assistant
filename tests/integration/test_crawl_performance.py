from __future__ import annotations

import json
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from nptu_assistant.crawlers.site_map import (
    FrontierPolicy,
    SiteCrawlStatus,
    SiteLinkType,
    SiteLinkUpsert,
    SitePageType,
    SitePageUpsert,
)
from nptu_assistant.crawlers.site_models import SearchPlan
from nptu_assistant.db.crawl_models import SiteCrawlAttempt
from nptu_assistant.db.crawl_scheduler import (
    SqlCrawlSchedulerRepository,
    due_pages_statement,
)
from nptu_assistant.db.models import SiteLink, SitePage
from nptu_assistant.db.site_map import SqlSiteMapRepository
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="需要 PostgreSQL live acceptance",
)


def _factory() -> tuple[sessionmaker[Session], object]:
    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _node_types(plan: dict[str, object]) -> set[str]:
    result: set[str] = set()
    if isinstance(plan.get("Node Type"), str):
        result.add(plan["Node Type"])
    children = plan.get("Plans", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                result.update(_node_types(child))
    return result


def _indexes(plan: dict[str, object]) -> set[str]:
    result: set[str] = set()
    for key in ("Index Name", "Index Cond", "Recheck Cond"):
        value = plan.get(key)
        if isinstance(value, str):
            result.add(value)
    children = plan.get("Plans", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                result.update(_indexes(child))
    return result


def _index_names(plan: dict[str, object]) -> set[str]:
    result: set[str] = set()
    value = plan.get("Index Name")
    if isinstance(value, str):
        result.add(value)
    children = plan.get("Plans", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                result.update(_index_names(child))
    return result


def _buffers(plan: dict[str, object]) -> dict[str, int]:
    totals = {
        "shared_hit_blocks": int(plan.get("Shared Hit Blocks", 0) or 0),
        "shared_read_blocks": int(plan.get("Shared Read Blocks", 0) or 0),
        "shared_dirtied_blocks": int(plan.get("Shared Dirtied Blocks", 0) or 0),
        "shared_written_blocks": int(plan.get("Shared Written Blocks", 0) or 0),
    }
    children = plan.get("Plans", [])
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                nested = _buffers(child)
                for key, value in nested.items():
                    totals[key] += value
    return totals


def test_postgres_claim_status_and_frontier_benchmark() -> None:
    factory, engine = _factory()
    prefix = f"https://perf-p31-{uuid4().hex}.nptu.edu.tw"
    now = datetime.now(timezone.utc)
    policy = FrontierPolicy(per_host_due_cap=1, per_host_active_cap=1)
    scheduler = SqlCrawlSchedulerRepository(factory, frontier_policy=policy)
    statements: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.strip():
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        hosts = tuple(f"perf-p31-{index}.nptu.edu.tw" for index in range(20))
        rows = []
        for index in range(10_000):
            host = hosts[index % len(hosts)]
            is_due = index < 2_000
            status = (
                SiteCrawlStatus.BLOCKED.value
                if index in {8_000, 8_001}
                else SiteCrawlStatus.EXCLUDED.value
                if index in {8_002, 8_003}
                else SiteCrawlStatus.DISCOVERED.value
            )
            rows.append(
                {
                    "id": uuid4(),
                    "canonical_url": f"{prefix}/{index}",
                    "host": host,
                    "path": f"/{index}",
                    "title": f"performance page {index}",
                    "page_type": SitePageType.GENERAL_PAGE.value,
                    "discovery_source": "internal_link",
                    "crawl_status": status,
                    "next_crawl_at": now - timedelta(minutes=1)
                    if is_due
                    else now + timedelta(hours=1),
                    "is_active": True,
                    "is_indexable": True,
                    "crawl_priority": 1,
                    "failure_count": 0,
                    "minimum_depth": 0,
                }
            )
        with factory.begin() as session:
            session.execute(SitePage.__table__.insert(), rows)
            # A bulk fixture bypasses PostgreSQL's normal auto-analyze
            # threshold. Refresh statistics before measuring the production
            # claim query so the benchmark reflects a maintained table rather
            # than an intentionally stale planner estimate.
            session.execute(text("ANALYZE site_pages"))
            active_ids = session.scalars(
                select(SitePage.id)
                .where(SitePage.canonical_url.like(f"{prefix}/%"))
                .order_by(SitePage.canonical_url)
                .limit(5)
            ).all()
            session.execute(
                SitePage.__table__.update()
                .where(SitePage.id.in_(active_ids))
                .values(
                    crawl_lease_owner="perf-active-worker",
                    crawl_lease_token=uuid4(),
                    crawl_lease_expires_at=now + timedelta(minutes=5),
                    crawl_status=SiteCrawlStatus.FETCHING.value,
                )
            )

        claim_statement = due_pages_statement(
            now=now,
            limit=20,
            frontier_policy=policy,
        )
        compiled = claim_statement.compile(
            dialect=engine.dialect,
            compile_kwargs={"literal_binds": True},
        )
        with engine.connect() as connection:
            plan_row = connection.execute(
                text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}")
            ).scalar_one()
        plan = plan_row[0] if isinstance(plan_row, list) else plan_row
        assert isinstance(plan, dict)
        claim_plan = plan["Plan"]
        assert isinstance(claim_plan, dict)
        planning_ms = float(plan["Planning Time"])
        execution_ms = float(plan["Execution Time"])
        returned_rows = int(claim_plan.get("Actual Rows", 0))
        claim_node_types = sorted(_node_types(claim_plan))
        claim_indexes = sorted(_indexes(claim_plan))

        claim_statement_start = len(statements)
        claim_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claim_batches = list(
                pool.map(
                    lambda worker: scheduler.claim_due(
                        owner=f"perf-worker-{worker}",
                        limit=20,
                        lease_duration=timedelta(minutes=5),
                        now=now,
                    ),
                    range(4),
                )
            )
        claim_elapsed_ms = (time.perf_counter() - claim_started) * 1000
        claims = [claim for batch in claim_batches for claim in batch]
        assert len({claim.page_id for claim in claims}) == len(claims)
        assert len({claim.token for claim in claims}) == len(claims)
        assert all(
            count <= 1 for count in Counter(claim.host for claim in claims).values()
        )
        assert planning_ms < 2_000
        assert execution_ms < 5_000
        assert claim_elapsed_ms < 15_000
        claim_statement_count = len(statements) - claim_statement_start

        status_started = time.perf_counter()
        status = scheduler.status(now=now)
        status_elapsed_ms = (time.perf_counter() - status_started) * 1000
        assert int(status["active_workers"]) >= 1
        assert int(status["leased"]) >= 5
        status_query = (
            select(func.count())
            .select_from(SitePage)
            .where(
                SitePage.is_active.is_(True),
                SitePage.is_indexable.is_(True),
                SitePage.next_crawl_at.is_not(None),
                SitePage.next_crawl_at <= now,
                SitePage.crawl_lease_expires_at <= now,
            )
        )
        with engine.connect() as connection:
            status_plan_row = connection.execute(
                text(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                    + str(
                        status_query.compile(
                            dialect=engine.dialect,
                            compile_kwargs={"literal_binds": True},
                        )
                    )
                )
            ).scalar_one()
        status_plan = (
            status_plan_row[0] if isinstance(status_plan_row, list) else status_plan_row
        )
        assert isinstance(status_plan, dict)
        status_root = status_plan["Plan"]
        assert isinstance(status_root, dict)
        assert status_elapsed_ms < 5_000

        frontier_prefix = f"{prefix}/frontier"
        frontier = SqlSiteMapRepository(
            factory,
            frontier_policy=FrontierPolicy(
                max_pending_total=20_000,
                per_host_pending_cap=10_000,
            ),
        )
        source = SitePageUpsert(canonical_url=f"{frontier_prefix}/source")
        links = tuple(
            SiteLinkUpsert(
                target=SitePageUpsert(
                    canonical_url=f"{frontier_prefix}/target-{index}",
                    next_crawl_at=now + timedelta(seconds=30),
                ),
                anchor_text=f"target {index}",
                link_type=SiteLinkType.CONTENT,
            )
            for index in range(100)
        )
        frontier_start = len(statements)
        frontier_started = time.perf_counter()
        frontier_result = frontier.persist_fetched_page(
            source,
            title="frontier source",
            content_hash="a" * 64,
            http_status=200,
            links=links,
            allow_unleased=True,
        )
        frontier_elapsed_ms = (time.perf_counter() - frontier_started) * 1000
        frontier_statement_count = len(statements) - frontier_start
        assert frontier_result.target_created == 100
        assert frontier_statement_count <= 9
        assert frontier_elapsed_ms < 10_000

        print(
            json.dumps(
                {
                    "dataset": {
                        "site_pages": 10_000,
                        "due_pages": 2_000,
                        "hosts": 20,
                        "active_leases": 5,
                    },
                    "claim": {
                        "planning_ms": planning_ms,
                        "execution_ms": execution_ms,
                        "returned_rows": returned_rows,
                        "runtime_ms": claim_elapsed_ms,
                        "statement_count": claim_statement_count,
                        "node_types": claim_node_types,
                        "indexes": claim_indexes,
                    },
                    "status": {
                        "runtime_ms": status_elapsed_ms,
                        "node_types": sorted(_node_types(status_root)),
                        "indexes": sorted(_indexes(status_root)),
                    },
                    "frontier": {
                        "runtime_ms": frontier_elapsed_ms,
                        "statement_count": frontier_statement_count,
                        "target_created": frontier_result.target_created,
                    },
                },
                ensure_ascii=False,
            )
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
        with factory.begin() as session:
            page_ids = select(SitePage.id).where(
                SitePage.canonical_url.like(f"{prefix}%")
            )
            session.execute(
                delete(SiteCrawlAttempt).where(
                    SiteCrawlAttempt.site_page_id.in_(page_ids)
                )
            )
            session.execute(
                delete(SitePage).where(SitePage.canonical_url.like(f"{prefix}%"))
            )
        engine.dispose()


def test_postgres_candidate_sql_bounded_with_lexical_recall() -> None:
    """Run the production candidate SQL against a representative live dataset."""

    factory, engine = _factory()
    prefix = f"p31-candidate-{uuid4().hex}"
    page_ids = [uuid4() for _ in range(5_000)]
    title_page_id = page_ids[1_001]
    path_page_id = page_ids[2_002]
    anchor_page_id = page_ids[3_003]
    concept_page_id = page_ids[4_004]
    invalid_page_ids = page_ids[:3]
    page_rows: list[dict[str, object]] = []
    for index, page_id in enumerate(page_ids):
        title = f"site page {index}"
        path = f"/{prefix}/{index}"
        if page_id in invalid_page_ids:
            title = "scholarship invalid filter"
        elif page_id == title_page_id:
            title = "scholarship application title recall"
        elif page_id == path_page_id:
            path = f"/{prefix}/financial-aid/path-recall"
        elif page_id == concept_page_id:
            title = "financial aid concept recall"
        page_rows.append(
            {
                "id": page_id,
                "canonical_url": f"https://www.nptu.edu.tw{path}",
                "host": "www.nptu.edu.tw",
                "path": path,
                "title": title,
                "page_type": SitePageType.GENERAL_PAGE.value,
                "discovery_source": "internal_link",
                "crawl_status": SiteCrawlStatus.DISCOVERED.value,
                "is_active": page_id not in invalid_page_ids,
                "is_indexable": page_id != invalid_page_ids[1],
            }
        )
    page_rows[2]["crawl_status"] = SiteCrawlStatus.BLOCKED.value

    link_rows: list[dict[str, object]] = []
    for source_index in range(5_000):
        for offset in range(1, 5):
            target_index = (source_index + offset) % 5_000
            link_rows.append(
                {
                    "id": uuid4(),
                    "source_page_id": page_ids[source_index],
                    "target_page_id": page_ids[target_index],
                    "anchor_text": (
                        "application anchor recall"
                        if page_ids[target_index] == anchor_page_id
                        else f"link {source_index}-{offset}"
                    ),
                    "link_type": SiteLinkType.CONTENT.value,
                }
            )

    repository = SqlSiteMapRepository(
        factory,
        site_map_query_max_seconds=0.75,
    )
    plan = SearchPlan(
        query="scholarship",
        search_queries=["scholarship", "application", "financial aid"],
        concepts=["student grant"],
        limit=20,
    )
    statement = repository.build_candidate_query(
        plan,
        scope=None,
        allowed_hosts=("nptu.edu.tw",),
        limit=20,
        dialect_name="postgresql",
    )
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}"
    try:
        with factory.begin() as session:
            session.execute(SitePage.__table__.insert(), page_rows)
            session.execute(SiteLink.__table__.insert(), link_rows)
            session.execute(text("ANALYZE site_pages"))
            session.execute(text("ANALYZE site_links"))

        explain_summaries: list[dict[str, object]] = []
        for _ in range(3):
            with engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT set_config("
                        "'pg_trgm.similarity_threshold', '0.10', true)"
                    )
                )
                raw_plan = connection.exec_driver_sql(explain_sql).scalar_one()
            payload = raw_plan[0] if isinstance(raw_plan, list) else raw_plan
            assert isinstance(payload, dict)
            root = payload["Plan"]
            assert isinstance(root, dict)
            explain_summaries.append(
                {
                    "planning_ms": float(payload["Planning Time"]),
                    "execution_ms": float(payload["Execution Time"]),
                    "node_types": sorted(_node_types(root)),
                    "indexes": sorted(_indexes(root)),
                    "index_names": sorted(_index_names(root)),
                    "buffers": _buffers(root),
                }
            )

        # Keep the default planner result above as the production evidence.
        # On a 5,000-row fixture PostgreSQL may quite reasonably prefer a
        # sequential scan.  A second EXPLAIN of the exact production
        # statement with sequential scans disabled verifies that the bounded
        # lexical predicates remain index-capable without changing runtime
        # behavior or hiding a default-plan regression.
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            connection.execute(
                text("SELECT set_config('pg_trgm.similarity_threshold', '0.10', true)")
            )
            raw_index_plan = connection.exec_driver_sql(explain_sql).scalar_one()
        index_payload = (
            raw_index_plan[0] if isinstance(raw_index_plan, list) else raw_index_plan
        )
        assert isinstance(index_payload, dict)
        index_root = index_payload["Plan"]
        assert isinstance(index_root, dict)
        index_probe = {
            "planning_ms": float(index_payload["Planning Time"]),
            "execution_ms": float(index_payload["Execution Time"]),
            "node_types": sorted(_node_types(index_root)),
            "index_names": sorted(_index_names(index_root)),
            "buffers": _buffers(index_root),
        }

        runtimes_ms: list[float] = []
        for _ in range(3):
            started = time.perf_counter()
            rows = repository.find_candidates(
                plan,
                scope=None,
                allowed_hosts=("nptu.edu.tw",),
                limit=20,
            )
            runtimes_ms.append((time.perf_counter() - started) * 1000)
        assert rows
        returned_urls = {candidate.canonical_url for candidate in rows}
        expected_ids = {title_page_id, path_page_id, anchor_page_id, concept_page_id}
        expected_urls = {
            str(row["canonical_url"]) for row in page_rows if row["id"] in expected_ids
        }
        assert expected_urls <= returned_urls, (
            f"missing lexical recall urls: {expected_urls - returned_urls}"
        )
        invalid_urls = {
            str(row["canonical_url"])
            for row in page_rows
            if row["id"] in invalid_page_ids
        }
        assert not returned_urls.intersection(invalid_urls)
        default_used_index_names = {
            name
            for summary in explain_summaries
            for name in summary["index_names"]
            if isinstance(name, str)
        }
        required_index_names = {
            "ix_site_pages_title_trgm",
            "ix_site_pages_path_trgm",
            "ix_site_links_anchor_text_trgm",
        }
        assert required_index_names <= set(index_probe["index_names"]), (
            "production candidate SQL has no usable lexical GIN index path: "
            f"missing={required_index_names - set(index_probe['index_names'])} "
            f"default_used={sorted(default_used_index_names)} "
            f"index_probe={sorted(index_probe['index_names'])}"
        )
        assert max(float(item["execution_ms"]) for item in explain_summaries) <= 750
        assert max(runtimes_ms) <= 1_000
        print(
            json.dumps(
                {
                    "dataset": {
                        "site_pages": 5_000,
                        "site_links": 20_000,
                        "hosts": 1,
                    },
                    "candidate": explain_summaries,
                    "default_used_indexes": sorted(default_used_index_names),
                    "index_probe": index_probe,
                    "production_runtime_ms": runtimes_ms,
                },
                ensure_ascii=False,
            )
        )
    finally:
        with factory.begin() as session:
            session.execute(
                delete(SitePage).where(SitePage.canonical_url.like(f"%/{prefix}/%"))
            )
        engine.dispose()
