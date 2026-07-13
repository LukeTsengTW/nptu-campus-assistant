from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.db.models import Announcement
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import AnnouncementSort


class FakeEmbedding:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1536 for _ in texts]


class FakeResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[object]:
        return self._rows


class FakeSession:
    def __init__(self, scalar_value: object | None = None) -> None:
        self.statements: list[object] = []
        self.scalar_value = scalar_value

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return FakeResult()

    def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_value


class FakeFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSession:
        return self.session


def sql(statement: object) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def make_retriever(session: FakeSession) -> SqlRetriever:
    return SqlRetriever(FakeFactory(session), FakeEmbedding())  # type: ignore[arg-type]


def test_newest_announcements_use_structured_limit_and_fixture_filter() -> None:
    session = FakeSession()

    result = make_retriever(session).search_announcements(
        query=None,
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=None,
        date_from=None,
        date_to=None,
    )
    statement = sql(session.statements[0])

    assert result == []
    assert "sources.name NOT ILIKE '%%fixture%%'" in statement
    assert "ORDER BY announcements.published_at DESC, announcements.last_crawled_at DESC" in statement
    assert "LIMIT 5" in statement


def test_relevance_announcements_use_similarity_and_structured_filters() -> None:
    session = FakeSession()

    make_retriever(session).search_announcements(
        query="獎學金",
        limit=3,
        sort=AnnouncementSort.RELEVANCE,
        unit="教務處",
        date_from=date(2026, 7, 1),
        date_to=date(2026, 7, 12),
    )
    statement = sql(session.statements[0])

    assert "similarity(announcements.title, '獎學金')" in statement
    assert "announcements.unit ILIKE '%%教務處%%'" in statement
    assert "announcements.published_at >= '2026-07-01'" in statement
    assert "announcements.published_at <= '2026-07-12'" in statement
    assert "ORDER BY score DESC, announcements.published_at DESC" in statement
    assert "LIMIT 3" in statement


def test_oldest_and_empty_relevance_have_deterministic_ordering() -> None:
    oldest_session = FakeSession()
    newest_session = FakeSession()

    make_retriever(oldest_session).search_announcements(
        query=None,
        limit=2,
        sort=AnnouncementSort.OLDEST,
        unit=None,
        date_from=None,
        date_to=None,
    )
    make_retriever(newest_session).search_announcements(
        query="",
        limit=2,
        sort=AnnouncementSort.RELEVANCE,
        unit=None,
        date_from=None,
        date_to=None,
    )

    assert "ORDER BY announcements.published_at ASC" in sql(oldest_session.statements[0])
    assert "ORDER BY announcements.published_at DESC, announcements.last_crawled_at DESC" in sql(
        newest_session.statements[0]
    )


def test_retriever_rejects_invalid_limits_and_date_ranges() -> None:
    retriever = make_retriever(FakeSession())

    for limit in (0, 21):
        with pytest.raises(ValueError, match="limit"):
            retriever.search_announcements(
                query=None,
                limit=limit,
                sort=AnnouncementSort.NEWEST,
                unit=None,
                date_from=None,
                date_to=None,
            )
    with pytest.raises(ValueError, match="日期"):
        retriever.search_announcements(
            query=None,
            limit=5,
            sort=AnnouncementSort.NEWEST,
            unit=None,
            date_from=date(2026, 7, 12),
            date_to=date(2026, 7, 1),
        )


def test_get_announcement_uses_id_and_returns_evidence() -> None:
    announcement_id = uuid.uuid4()
    announcement = Announcement(
        id=announcement_id,
        source_id=uuid.uuid4(),
        title="測試公告",
        unit="教務處",
        category=None,
        published_at=date(2026, 7, 12),
        deadline_at=None,
        canonical_url="https://www.nptu.edu.tw/announcement/1",
        body="完整公告內容",
        warning=None,
        content_hash="a" * 64,
        last_crawled_at=datetime.now(timezone.utc),
    )
    session = FakeSession(announcement)

    evidence = make_retriever(session).get_announcement(str(announcement_id))

    assert evidence is not None
    assert evidence.id == str(announcement_id)
    assert evidence.kind is AnswerType.ANNOUNCEMENT
    statement = sql(session.statements[0])
    assert str(announcement_id) in statement
    assert "sources.name NOT ILIKE '%%fixture%%'" in statement
    assert make_retriever(FakeSession()).get_announcement("not-a-uuid") is None


def test_document_search_keeps_vector_similarity_keyword_similarity_and_current_filter() -> None:
    session = FakeSession()

    result = make_retriever(session).search_documents(query="學貸申請", limit=6)

    assert result == []
    assert len(session.statements) == 2
    vector_statement, keyword_statement = map(sql, session.statements)
    assert "<=>" in vector_statement
    assert "documents.is_current IS true" in vector_statement
    assert "similarity(document_chunks.content, '學貸申請')" in keyword_statement
    assert "similarity(documents.title, '學貸申請')" in keyword_statement
