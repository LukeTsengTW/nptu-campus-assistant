from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
import logging
import threading
import time
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuListingItem,
    NptuSitePage,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.site_models import SearchDiagnostics
from nptu_assistant.db.models import SiteSearchCacheRecord

if TYPE_CHECKING:
    from nptu_assistant.crawlers.site_search import SiteSearchResult


logger = logging.getLogger(__name__)
SITE_SEARCH_CACHE_SCHEMA_VERSION = "p1-v1"


class SiteSearchCache(Protocol):
    def get(self, cache_key: str) -> SiteSearchResult | None: ...

    def set(
        self, cache_key: str, result: SiteSearchResult, ttl_seconds: int
    ) -> None: ...


def _page_payload(page: NptuSitePage) -> dict[str, object]:
    return {
        "title": page.title,
        "canonical_url": page.canonical_url,
        "body": page.body,
        "published_at": page.published_at.isoformat() if page.published_at else None,
        "links": list(page.links),
        "link_texts": [[url, label] for url, label in page.link_texts],
        "headings": list(page.headings),
        "score": page.score,
        "role": page.role.value,
        "announcement_items": [
            {
                "title": item.title,
                "canonical_url": item.canonical_url,
                "published_at": (
                    item.published_at.isoformat() if item.published_at else None
                ),
                "summary": item.summary,
                "anchor_text": item.anchor_text,
                "order": item.order,
            }
            for item in page.announcement_items
        ],
    }


def serialize_site_search_result(result: SiteSearchResult) -> dict[str, object]:
    diagnostics = result.diagnostics
    return {
        "pages": [_page_payload(page) for page in result.pages],
        "diagnostics": {
            "discovered_count": diagnostics.discovered_count,
            "fetched_count": diagnostics.fetched_count,
            "relevant_success_count": diagnostics.relevant_success_count,
            "relevant_fetch_failure_count": diagnostics.relevant_fetch_failure_count,
            "unrelated_fetch_failure_count": diagnostics.unrelated_fetch_failure_count,
            "timed_out_candidate_count": diagnostics.timed_out_candidate_count,
            "skipped_candidate_count": diagnostics.skipped_candidate_count,
            "query_timed_out": diagnostics.query_timed_out,
            "highest_success_score": diagnostics.highest_success_score,
            "highest_fetch_failure_score": diagnostics.highest_fetch_failure_score,
            "highest_unattempted_score": diagnostics.highest_unattempted_score,
        },
    }


def _required_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("cache payload 欄位格式錯誤")
    return value


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    return date.fromisoformat(_required_text(value))


def _page_from_payload(value: object) -> NptuSitePage:
    if not isinstance(value, dict):
        raise ValueError("cache page payload 格式錯誤")
    links = value.get("links", [])
    link_texts = value.get("link_texts", [])
    headings = value.get("headings", [])
    items = value.get("announcement_items", [])
    if not isinstance(links, list) or not isinstance(headings, list):
        raise ValueError("cache page list 格式錯誤")
    if not all(isinstance(item, str) for item in links + headings):
        raise ValueError("cache page list 格式錯誤")
    if not isinstance(link_texts, list) or not all(
        isinstance(item, list)
        and len(item) == 2
        and all(isinstance(part, str) for part in item)
        for item in link_texts
    ):
        raise ValueError("cache link text 格式錯誤")
    if not isinstance(items, list):
        raise ValueError("cache announcement item 格式錯誤")
    announcement_items = tuple(
        NptuListingItem(
            title=_required_text(item["title"]),
            canonical_url=_required_text(item["canonical_url"]),
            published_at=_optional_date(item.get("published_at")),
            summary=_required_text(item["summary"]),
            anchor_text=_required_text(item["anchor_text"]),
            order=int(item["order"]),
        )
        for item in items
        if isinstance(item, dict)
    )
    if len(announcement_items) != len(items):
        raise ValueError("cache announcement item 欄位錯誤")
    role = UnitAnnouncementPageRole(_required_text(value.get("role", "other")))
    return NptuSitePage(
        title=_required_text(value["title"]),
        canonical_url=_required_text(value["canonical_url"]),
        body=_required_text(value["body"]),
        published_at=_optional_date(value.get("published_at")),
        links=tuple(cast(list[str], links)),
        link_texts=tuple((item[0], item[1]) for item in link_texts),
        headings=tuple(cast(list[str], headings)),
        score=float(value.get("score", 0.0)),
        role=role,
        announcement_items=announcement_items,
    )


def deserialize_site_search_result(payload: object) -> SiteSearchResult:
    if not isinstance(payload, dict):
        raise ValueError("cache payload 必須是 object")
    pages = payload.get("pages")
    raw_diagnostics = payload.get("diagnostics")
    if not isinstance(pages, list) or not isinstance(raw_diagnostics, dict):
        raise ValueError("cache payload 缺少欄位")
    diagnostics = SearchDiagnostics(
        discovered_count=int(raw_diagnostics.get("discovered_count", 0)),
        fetched_count=int(raw_diagnostics.get("fetched_count", 0)),
        relevant_success_count=int(raw_diagnostics.get("relevant_success_count", 0)),
        relevant_fetch_failure_count=int(
            raw_diagnostics.get("relevant_fetch_failure_count", 0)
        ),
        unrelated_fetch_failure_count=int(
            raw_diagnostics.get("unrelated_fetch_failure_count", 0)
        ),
        timed_out_candidate_count=int(
            raw_diagnostics.get("timed_out_candidate_count", 0)
        ),
        skipped_candidate_count=int(raw_diagnostics.get("skipped_candidate_count", 0)),
        query_timed_out=bool(raw_diagnostics.get("query_timed_out", False)),
        highest_success_score=cast(
            float | None, raw_diagnostics.get("highest_success_score")
        ),
        highest_fetch_failure_score=cast(
            float | None, raw_diagnostics.get("highest_fetch_failure_score")
        ),
        highest_unattempted_score=cast(
            float | None, raw_diagnostics.get("highest_unattempted_score")
        ),
    )
    from nptu_assistant.crawlers.site_search import SiteSearchResult

    return SiteSearchResult(
        tuple(_page_from_payload(page) for page in pages),
        diagnostics,
    )


class InMemorySiteSearchCache:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._items: dict[str, tuple[float, SiteSearchResult]] = {}
        self._lock = threading.Lock()

    def get(self, cache_key: str) -> SiteSearchResult | None:
        with self._lock:
            item = self._items.get(cache_key)
            if item is None:
                return None
            if item[0] <= self._clock():
                self._items.pop(cache_key, None)
                return None
            return item[1]

    def set(self, cache_key: str, result: SiteSearchResult, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            self._items[cache_key] = (self._clock() + ttl_seconds, result)


class PostgresSiteSearchCache:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def get(self, cache_key: str) -> SiteSearchResult | None:
        try:
            with self._factory() as session:
                record = session.scalar(
                    select(SiteSearchCacheRecord).where(
                        SiteSearchCacheRecord.cache_key == cache_key,
                        SiteSearchCacheRecord.schema_version
                        == SITE_SEARCH_CACHE_SCHEMA_VERSION,
                        SiteSearchCacheRecord.expires_at > datetime.now(timezone.utc),
                    )
                )
            if record is None:
                return None
            return deserialize_site_search_result(record.payload)
        except Exception:
            logger.exception("持久化網站搜尋快取讀取失敗")
            return None

    def set(self, cache_key: str, result: SiteSearchResult, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        try:
            now = datetime.now(timezone.utc)
            payload = serialize_site_search_result(result)
            with self._factory() as session:
                bind = session.get_bind()
                if bind.dialect.name == "postgresql":
                    statement = postgres_insert(SiteSearchCacheRecord).values(
                        cache_key=cache_key,
                        payload=payload,
                        created_at=now,
                        expires_at=now + timedelta(seconds=ttl_seconds),
                        schema_version=SITE_SEARCH_CACHE_SCHEMA_VERSION,
                    )
                    statement = statement.on_conflict_do_update(
                        index_elements=[SiteSearchCacheRecord.cache_key],
                        set_={
                            "payload": statement.excluded.payload,
                            "created_at": statement.excluded.created_at,
                            "expires_at": statement.excluded.expires_at,
                            "schema_version": statement.excluded.schema_version,
                        },
                    )
                    session.execute(statement)
                else:
                    record = session.get(SiteSearchCacheRecord, cache_key)
                    if record is None:
                        session.add(
                            SiteSearchCacheRecord(
                                cache_key=cache_key,
                                payload=payload,
                                created_at=now,
                                expires_at=now + timedelta(seconds=ttl_seconds),
                                schema_version=SITE_SEARCH_CACHE_SCHEMA_VERSION,
                            )
                        )
                    else:
                        record.payload = payload
                        record.created_at = now
                        record.expires_at = now + timedelta(seconds=ttl_seconds)
                        record.schema_version = SITE_SEARCH_CACHE_SCHEMA_VERSION
                session.commit()
        except Exception:
            logger.exception("持久化網站搜尋快取寫入失敗")


class LayeredSiteSearchCache:
    def __init__(
        self,
        l1: InMemorySiteSearchCache,
        l2: SiteSearchCache,
        *,
        ttl_seconds: int = 300,
    ) -> None:
        self._l1 = l1
        self._l2 = l2
        self._ttl_seconds = ttl_seconds

    def get(self, cache_key: str) -> SiteSearchResult | None:
        result = self._l1.get(cache_key)
        if result is not None:
            return result
        result = self._l2.get(cache_key)
        if result is not None:
            self._l1.set(cache_key, result, self._ttl_seconds)
        return result

    def set(self, cache_key: str, result: SiteSearchResult, ttl_seconds: int) -> None:
        self._l1.set(cache_key, result, ttl_seconds)
        self._l2.set(cache_key, result, ttl_seconds)


class SingleFlightLease(Protocol):
    def release(self) -> None: ...


class SingleFlightRunner(Protocol):
    def acquire(self, cache_key: str) -> SingleFlightLease | None: ...


def _lock_key(cache_key: str) -> int:
    import hashlib

    return int.from_bytes(
        hashlib.sha256(cache_key.encode("utf-8")).digest()[:8],
        "big",
        signed=True,
    )


class _LocalLease:
    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


class _PostgresLease:
    def __init__(self, session: Session, key: int) -> None:
        self._session = session
        self._key = key

    def release(self) -> None:
        try:
            self._session.scalar(select(func.pg_advisory_unlock(self._key)))
        except Exception:
            logger.exception("PostgreSQL single-flight lock 釋放失敗")
        finally:
            self._session.close()


class SingleFlightCoordinator:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory
        self._local_locks: dict[str, threading.Lock] = {}
        self._local_guard = threading.Lock()

    def try_acquire(self, cache_key: str) -> SingleFlightLease | None:
        session = self._factory()
        get_bind = getattr(session, "get_bind", None)
        if get_bind is not None and get_bind().dialect.name == "postgresql":
            key = _lock_key(cache_key)
            acquired = bool(session.scalar(select(func.pg_try_advisory_lock(key))))
            if acquired:
                return _PostgresLease(session, key)
            session.close()
            return None
        close = getattr(session, "close", None)
        if callable(close):
            close()
        with self._local_guard:
            lock = self._local_locks.setdefault(cache_key, threading.Lock())
        if lock.acquire(blocking=False):
            return _LocalLease(lock)
        return None


class SingleFlightSearchRunner:
    """跨 PostgreSQL process 去重昂貴 live search。"""

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._coordinator = SingleFlightCoordinator(factory)

    def acquire(self, cache_key: str) -> SingleFlightLease | None:
        return self._coordinator.try_acquire(cache_key)
