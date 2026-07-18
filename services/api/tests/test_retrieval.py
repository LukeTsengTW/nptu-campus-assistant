from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.crawlers.config import SiteSearchConfig
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
    SearchPlan,
)
from nptu_assistant.crawlers.site_search import SitePageIngestionService
from nptu_assistant.db.models import Announcement
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import AnnouncementSort


class FakeEmbedding:
    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        return [[0.0] * 1536 for _ in texts]


class RecordingEmbedding(FakeEmbedding):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        self.calls.append(texts)
        return [[float(index)] * 1536 for index, _text in enumerate(texts)]


class FakeResult:
    def __init__(self, rows: list[object] | None = None) -> None:
        self._rows = rows or []

    def all(self) -> list[object]:
        return self._rows


class FakeSession:
    def __init__(
        self,
        scalar_value: object | None = None,
        *,
        result_batches: list[list[object]] | None = None,
    ) -> None:
        self.statements: list[object] = []
        self.scalar_value = scalar_value
        self.result_batches = list(result_batches or [])

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        rows = self.result_batches.pop(0) if self.result_batches else []
        return FakeResult(rows)

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


def make_retriever(
    session: FakeSession,
    embedding: FakeEmbedding | None = None,
) -> SqlRetriever:
    return SqlRetriever(  # type: ignore[arg-type]
        FakeFactory(session),
        embedding or FakeEmbedding(),
    )


def document_row(
    *,
    title: str,
    content: str,
    url: str,
    score: float = 0.82,
) -> tuple[object, object, object, float]:
    document_id = uuid.uuid4()
    chunk = SimpleNamespace(id=uuid.uuid4(), document_id=document_id, content=content)
    document = SimpleNamespace(
        id=document_id,
        title=title,
        canonical_url=url,
        published_at=None,
    )
    source = SimpleNamespace(unit="國立屏東大學")
    return chunk, document, source, score


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
    assert (
        "ORDER BY announcements.published_at DESC, announcements.last_crawled_at DESC"
        in statement
    )
    assert "LIMIT 5" in statement


def test_newest_keyword_search_is_limited_to_current_canonical_urls() -> None:
    session = FakeSession()
    urls = (
        "https://www.nptu.edu.tw/p/406-1000-200001.php",
        "https://csai.nptu.edu.tw/p/406-1096-200002.php",
    )

    make_retriever(session).search_announcements(
        query="電腦科學與人工智慧學系",
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=None,
        date_from=None,
        date_to=None,
        canonical_urls=urls,
    )
    statement = sql(session.statements[0])

    assert "announcements.canonical_url IN" in statement
    assert urls[0] in statement
    assert urls[1] in statement
    assert "ORDER BY announcements.published_at DESC" in statement
    assert "CASE announcements.canonical_url" in statement
    assert "announcements.last_crawled_at DESC" not in statement
    assert "LIMIT" not in statement
    assert "announcements.unit ILIKE" not in statement
    assert "sources.name NOT ILIKE" not in statement


def test_successful_empty_keyword_scope_returns_no_announcements() -> None:
    session = FakeSession()

    result = make_retriever(session).search_announcements(
        query="查無結果關鍵字",
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=None,
        date_from=None,
        date_to=None,
        canonical_urls=(),
    )

    assert result == []
    assert session.statements == []


def test_failed_live_search_fallback_requires_database_relevance() -> None:
    session = FakeSession()

    make_retriever(session).search_announcements(
        query="電腦科學與人工智慧學系",
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=None,
        date_from=None,
        date_to=None,
        canonical_urls=None,
    )
    statement = sql(session.statements[0]).lower()

    assert "similarity(announcements.title, '電腦科學與人工智慧學系')" in statement
    assert "similarity(announcements.body, '電腦科學與人工智慧學系')" in statement
    assert (
        "similarity(coalesce(announcements.unit, ''), '電腦科學與人工智慧學系')"
        in statement
    )
    assert ">= 0.1" in statement
    assert "order by announcements.published_at desc" in statement


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

    assert "ORDER BY announcements.published_at ASC" in sql(
        oldest_session.statements[0]
    )
    assert (
        "ORDER BY announcements.published_at DESC, announcements.last_crawled_at DESC"
        in sql(newest_session.statements[0])
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


def test_document_search_keeps_vector_similarity_keyword_similarity_and_current_filter() -> (
    None
):
    session = FakeSession()

    result = make_retriever(session).search_documents(query="學貸申請", limit=6)

    assert result == []
    assert len(session.statements) == 2
    vector_statement, keyword_statement = map(sql, session.statements)
    assert "<=>" in vector_statement
    assert "documents.is_current IS true" in vector_statement
    assert "similarity(document_chunks.content, '學貸申請')" in keyword_statement
    assert "similarity(documents.title, '學貸申請')" in keyword_statement


def test_document_multi_query_retrieval_finds_different_admission_wording() -> None:
    search_plan = SearchPlan(
        query="個人申請新生資訊",
        search_queries=["大學申請入學", "申請入學新生報到"],
        concepts=["個人申請", "申請入學", "新生", "報到"],
        limit=6,
    )
    row = document_row(
        title="大學申請入學新生專區",
        content="錄取生應依規定完成網路報到。",
        url="https://www.nptu.edu.tw/admission/application",
    )
    session = FakeSession(
        result_batches=[[], [], [row], [row], [], []],
    )
    embedding = RecordingEmbedding()

    evidence = make_retriever(session, embedding).search_documents_with_plan(
        plan=search_plan,
        limit=6,
    )

    assert embedding.calls == [list(search_plan.retrieval_queries)]
    assert len(session.statements) == len(search_plan.retrieval_queries) * 2
    assert [item.title for item in evidence] == ["大學申請入學新生專區"]
    assert 0.0 <= evidence[0].score <= 1.0
    current_config = SiteSearchConfig(
        enabled=True,
        seed_urls=["https://www.nptu.edu.tw/"],
        allowed_hosts=["nptu.edu.tw"],
        database_min_results=1,
        database_min_content_chars=1,
    )
    ingestor = SitePageIngestionService(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        embedding,
        current_config,
    )
    assert ingestor.should_search_live(evidence) is False


def test_document_multi_query_retrieval_generalizes_to_dormitory_billing() -> None:
    search_plan = SearchPlan(
        query="學生宿舍冷氣費怎麼計算",
        search_queries=["宿舍電費計價", "學生宿舍用電收費標準"],
        concepts=["學生宿舍", "冷氣", "電費", "收費"],
        limit=6,
    )
    row = document_row(
        title="住宿服務中心學生宿舍用電計費辦法",
        content="宿舍用電依度數與公告收費標準計算。",
        url="https://staf-life.nptu.edu.tw/dormitory/electricity",
    )
    session = FakeSession(
        result_batches=[[], [], [row], [row], [row], []],
    )
    embedding = RecordingEmbedding()

    evidence = make_retriever(session, embedding).search_documents_with_plan(
        plan=search_plan,
        limit=6,
    )

    assert embedding.calls == [list(search_plan.retrieval_queries)]
    assert [item.title for item in evidence] == ["住宿服務中心學生宿舍用電計費辦法"]
    assert evidence[0].score >= 0.58


def test_document_multi_query_embedding_uses_remaining_live_deadline() -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            return self.value

    class ExpiringEmbedding(FakeEmbedding):
        def __init__(self, clock: FakeClock) -> None:
            self.clock = clock
            self.timeouts: list[float | None] = []

        def embed(
            self,
            texts: list[str],
            *,
            timeout_seconds: float | None = None,
        ) -> list[list[float]]:
            del texts
            self.timeouts.append(timeout_seconds)
            self.clock.value += 1.1
            raise RuntimeError("embedding timeout")

    clock = FakeClock()
    embedding = ExpiringEmbedding(clock)
    deadline = SearchDeadline.after(1.0, clock=clock)

    with pytest.raises(SearchDeadlineExceeded):
        make_retriever(FakeSession(), embedding).search_documents_with_plan(
            plan=SearchPlan.from_query("校務資訊"),
            limit=6,
            deadline=deadline,
        )

    assert embedding.timeouts == [pytest.approx(1.0)]
