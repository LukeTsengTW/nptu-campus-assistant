from __future__ import annotations

from enum import StrEnum


class QuestionRoute(StrEnum):
    DOCUMENT = "document"
    ANNOUNCEMENT = "announcement"
    MIXED = "mixed"


_ANNOUNCEMENT_TERMS = ("公告", "最新", "最近", "近期", "截止", "報名", "徵才", "活動")
_DOCUMENT_TERMS = ("辦法", "規章", "規定", "學分", "資格", "流程", "要點")


def route_question(question: str) -> QuestionRoute:
    has_announcement = any(term in question for term in _ANNOUNCEMENT_TERMS)
    has_document = any(term in question for term in _DOCUMENT_TERMS)
    if has_announcement and has_document:
        return QuestionRoute.MIXED
    if has_announcement:
        return QuestionRoute.ANNOUNCEMENT
    return QuestionRoute.DOCUMENT
