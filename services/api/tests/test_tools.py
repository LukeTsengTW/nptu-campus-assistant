from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.search import KeywordIngestionResult
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.tools import (
    AnnouncementSort,
    GetAnnouncementArguments,
    SearchAnnouncementsArguments,
    SearchDocumentsArguments,
    ToolExecutor,
    tool_definitions,
)


class StubRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def search_announcements(self, **kwargs: object) -> list[Evidence]:
        self.calls.append(("search_announcements", kwargs))
        return [
            Evidence(
                id="announcement-1",
                kind=AnswerType.ANNOUNCEMENT,
                title="測試公告",
                url="https://www.nptu.edu.tw/announcement/1",
                unit="教務處",
                published_at=date(2026, 7, 12),
                content="公告內容",
                score=0.9,
            )
        ]

    def search_documents(self, **kwargs: object) -> list[Evidence]:
        self.calls.append(("search_documents", kwargs))
        return []

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        self.calls.append(("get_announcement", announcement_id))
        return None


class StubKeywordIngestor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    def ingest(self, query: str) -> KeywordIngestionResult:
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("official search unavailable")
        return KeywordIngestionResult(
            retrieval_query=self.normalize(query),
            summary=CrawlSummary(created=1),
            warning=None,
        )

    def normalize(self, text: str) -> str:
        return text.replace("電科系", "電腦科學與人工智慧學系")


def test_tool_definitions_are_strict_and_require_every_property() -> None:
    definitions = {item["name"]: item for item in tool_definitions()}

    assert set(definitions) == {
        "search_announcements",
        "search_documents",
        "get_announcement",
    }
    for definition in definitions.values():
        assert definition["type"] == "function"
        assert definition["strict"] is True
        parameters = definition["parameters"]
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) == set(parameters["properties"])


def test_search_announcement_arguments_validate_limits_dates_and_extra_fields() -> None:
    parsed = SearchAnnouncementsArguments.model_validate(
        {
            "query": None,
            "limit": 5,
            "sort": "newest",
            "unit": None,
            "date_from": "2026-07-01",
            "date_to": "2026-07-12",
        }
    )

    assert parsed.sort is AnnouncementSort.NEWEST
    assert parsed.date_from == date(2026, 7, 1)

    for invalid in (
        {"query": None, "limit": 0, "sort": "newest", "unit": None, "date_from": None, "date_to": None},
        {"query": None, "limit": 21, "sort": "newest", "unit": None, "date_from": None, "date_to": None},
        {"query": None, "limit": 5, "sort": "unknown", "unit": None, "date_from": None, "date_to": None},
        {"query": None, "limit": 5, "sort": "newest", "unit": None, "date_from": "2026-07-12", "date_to": "2026-07-01"},
        {"query": None, "limit": 5, "sort": "newest", "unit": None, "date_from": None, "date_to": None, "sql": "select 1"},
    ):
        with pytest.raises(ValidationError):
            SearchAnnouncementsArguments.model_validate(invalid)


def test_other_tool_arguments_reject_missing_and_extra_fields() -> None:
    assert SearchDocumentsArguments.model_validate({"query": "學貸", "limit": 6}).limit == 6
    assert GetAnnouncementArguments.model_validate({"announcement_id": "announcement-1"}).announcement_id == "announcement-1"

    with pytest.raises(ValidationError):
        SearchDocumentsArguments.model_validate({"query": "學貸"})
    with pytest.raises(ValidationError):
        GetAnnouncementArguments.model_validate({"announcement_id": "announcement-1", "extra": True})


def test_executor_validates_arguments_and_serializes_safe_evidence() -> None:
    retriever = StubRetriever()
    executor = ToolExecutor(retriever)

    result = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "人工智慧",
                "limit": 3,
                "sort": "relevance",
                "unit": None,
                "date_from": None,
                "date_to": None,
            }
        ),
    )
    payload = json.loads(result.output)

    assert result.evidence[0].id == "announcement-1"
    assert payload["count"] == 1
    assert payload["results"][0] == {
        "id": "announcement-1",
        "kind": "announcement",
        "title": "測試公告",
        "url": "https://www.nptu.edu.tw/announcement/1",
        "unit": "教務處",
        "published_at": "2026-07-12",
        "content": "公告內容",
        "score": 0.9,
    }
    assert retriever.calls[0][0] == "search_announcements"


def test_executor_returns_structured_errors_without_executing_unknown_tools() -> None:
    retriever = StubRetriever()
    executor = ToolExecutor(retriever)

    invalid = json.loads(executor.execute("search_documents", "not-json").output)
    unknown = json.loads(executor.execute("drop_database", "{}").output)

    assert invalid["error"]["code"] == "invalid_tool_arguments"
    assert unknown["error"]["code"] == "unknown_tool"
    assert retriever.calls == []


def test_executor_ingests_keyword_before_database_search_and_normalizes_filters() -> None:
    retriever = StubRetriever()
    ingestor = StubKeywordIngestor()
    executor = ToolExecutor(retriever, keyword_ingestor=ingestor)

    result = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "電科系 獎學金",
                "limit": 3,
                "sort": "relevance",
                "unit": "電科系",
                "date_from": None,
                "date_to": None,
            }
        ),
    )

    assert result.warning is None
    assert ingestor.queries == ["電科系 獎學金"]
    assert retriever.calls[0] == (
        "search_announcements",
        {
            "query": "電腦科學與人工智慧學系 獎學金",
            "limit": 3,
            "sort": AnnouncementSort.RELEVANCE,
            "unit": "電腦科學與人工智慧學系",
            "date_from": None,
            "date_to": None,
        },
    )


def test_executor_does_not_ingest_null_query_and_falls_back_on_ingestion_failure() -> None:
    retriever = StubRetriever()
    ingestor = StubKeywordIngestor(fail=True)
    executor = ToolExecutor(retriever, keyword_ingestor=ingestor)

    failed = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "電科系",
                "limit": 3,
                "sort": "relevance",
                "unit": None,
                "date_from": None,
                "date_to": None,
            }
        ),
    )
    executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": None,
                "limit": 3,
                "sort": "oldest",
                "unit": None,
                "date_from": None,
                "date_to": None,
            }
        ),
    )

    assert failed.warning == "本次官網公告搜尋失敗，以下內容來自資料庫最後成功收錄的資料。"
    assert ingestor.queries == ["電科系"]
    assert len(retriever.calls) == 2
    assert retriever.calls[0][1]["query"] == "電腦科學與人工智慧學系"
