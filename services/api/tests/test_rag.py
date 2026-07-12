from __future__ import annotations

from datetime import date

from nptu_assistant.api.schemas import AnswerType, Confidence
from nptu_assistant.providers.fake import FakeLlmProvider
from nptu_assistant.rag.models import Evidence, GeneratedAnswer
from nptu_assistant.rag.retrieval import is_fixture_source, normalize_announcement_keyword
from nptu_assistant.rag.routing import QuestionRoute, route_question
from nptu_assistant.rag.service import ChatService, confidence_for_score


class StubRetriever:
    def __init__(self, evidence: list[Evidence]) -> None:
        self.evidence = evidence

    def search(self, question: str, route: QuestionRoute) -> list[Evidence]:
        return self.evidence


class StubLlm:
    def __init__(self, generated: GeneratedAnswer) -> None:
        self.generated = generated

    def generate(self, question: str, evidence: list[Evidence]) -> GeneratedAnswer:
        del question, evidence
        return self.generated


def test_route_question_detects_announcement_intent() -> None:
    assert route_question("最近有哪些報名公告？") is QuestionRoute.ANNOUNCEMENT
    assert route_question("學分抵免辦法是什麼？") is QuestionRoute.DOCUMENT


def test_latest_announcement_queries_use_recency_fallback() -> None:
    assert normalize_announcement_keyword("幫我查最新公告") == ""
    assert normalize_announcement_keyword("最近有哪些公告？") == ""
    assert normalize_announcement_keyword("公告") == ""
    assert normalize_announcement_keyword("獎學金公告") == "獎學金"


def test_fixture_sources_are_not_public_announcement_evidence() -> None:
    assert is_fixture_source("local-fixture")
    assert is_fixture_source("integration-fixture-abc")
    assert not is_fixture_source("nptu-overview")


def test_confidence_thresholds() -> None:
    assert confidence_for_score(0.8) is Confidence.HIGH
    assert confidence_for_score(0.6) is Confidence.MEDIUM
    assert confidence_for_score(0.4) is Confidence.LOW


def test_chat_returns_insufficient_without_evidence() -> None:
    service = ChatService(StubRetriever([]), FakeLlmProvider())

    response = service.answer("學分抵免辦法是什麼？")

    assert response.answer_type is AnswerType.INSUFFICIENT_INFORMATION
    assert response.sources == []
    assert "資料不足" in response.answer


def test_chat_uses_only_database_evidence_urls() -> None:
    evidence = Evidence(
        id="doc-1",
        kind=AnswerType.OFFICIAL_DOCUMENT,
        title="學分抵免辦法",
        url="https://www.nptu.edu.tw/rule",
        unit="教務處",
        published_at=date(2026, 1, 1),
        content="符合規定者可提出申請。",
        score=0.8,
    )
    service = ChatService(StubRetriever([evidence]), FakeLlmProvider())

    response = service.answer("學分抵免辦法是什麼？")

    assert response.sources[0].url == "https://www.nptu.edu.tw/rule"
    assert response.answer_type is AnswerType.OFFICIAL_DOCUMENT


def test_chat_rejects_unknown_model_source_ids() -> None:
    evidence = Evidence(
        id="doc-1",
        kind=AnswerType.OFFICIAL_DOCUMENT,
        title="測試文件",
        url="https://www.nptu.edu.tw/rule",
        unit="測試單位",
        published_at=None,
        content="測試內容",
        score=0.8,
    )
    service = ChatService(
        StubRetriever([evidence]),
        StubLlm(GeneratedAnswer(answer="無根據回答", used_source_ids=["unknown-id"])),
    )

    response = service.answer("測試問題")

    assert response.answer_type is AnswerType.INSUFFICIENT_INFORMATION
    assert response.sources == []


def test_chat_removes_internal_source_ids_from_user_facing_answer() -> None:
    source_id = "394a51a1-c0fc-4b96-a81f-f2acd9bd46e4"
    evidence = Evidence(
        id=source_id,
        kind=AnswerType.ANNOUNCEMENT,
        title="115學年度申請公告",
        url="https://www.nptu.edu.tw/announcement",
        unit="教務處",
        published_at=date(2026, 7, 10),
        content="申請公告內容",
        score=0.8,
    )
    service = ChatService(
        StubRetriever([evidence]),
        StubLlm(
            GeneratedAnswer(
                answer=f"115學年度申請公告 [{source_id}]",
                used_source_ids=[source_id],
            )
        ),
    )

    response = service.answer("最近有哪些公告？")

    assert source_id not in response.answer
    assert response.answer == "115學年度申請公告"
    assert response.sources[0].title == "115學年度申請公告"


def test_chat_preserves_conflict_warning_from_grounded_answer() -> None:
    evidence = Evidence(
        id="announcement-1",
        kind=AnswerType.ANNOUNCEMENT,
        title="測試公告",
        url="https://www.nptu.edu.tw/announcement",
        unit="測試單位",
        published_at=None,
        content="來源內容",
        score=0.7,
    )
    service = ChatService(
        StubRetriever([evidence]),
        StubLlm(
            GeneratedAnswer(
                answer="依據來源回答",
                used_source_ids=["announcement-1"],
                warning="兩份來源的日期互相矛盾",
            )
        ),
    )

    response = service.answer("測試問題")

    assert response.warning == "兩份來源的日期互相矛盾"
    assert response.confidence is Confidence.MEDIUM
