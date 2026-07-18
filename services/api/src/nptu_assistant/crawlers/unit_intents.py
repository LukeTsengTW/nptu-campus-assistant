from __future__ import annotations

from enum import StrEnum
import re

from nptu_assistant.crawlers.official_units import OfficialUnitDirectory


ANNOUNCEMENT_INTENT_TERMS = (
    "最新公告",
    "最近公告",
    "近期公告",
    "公告",
    "最新消息",
    "最近消息",
    "近期消息",
    "消息",
    "最新資訊",
    "最近資訊",
    "近期資訊",
    "最新動態",
    "最近動態",
    "近期動態",
    "動態",
    "通知",
)
LATEST_ANNOUNCEMENT_TERMS = (
    "最新公告",
    "最近公告",
    "近期公告",
    "最新消息",
    "最近消息",
    "近期消息",
    "最新資訊",
    "最近資訊",
    "近期資訊",
    "最新動態",
    "最近動態",
    "近期動態",
)
HOMEPAGE_INTENT_TERMS = (
    "官方網站",
    "官方頁面",
    "官網",
    "系網",
    "首頁",
)

_OPERATION_TERMS = (
    "請問",
    "麻煩",
    "幫我看看",
    "幫我看",
    "幫我查",
    "幫我",
    "想知道",
    "告訴我",
    "查詢",
    "搜尋",
    "搜索",
    "列出",
    "看看",
    "請",
    "查",
    "找",
    "看",
)
_COUNT_PATTERN = re.compile(
    r"(?:前\s*)?(?:\d{1,3}|[零一二三四五六七八九十百兩]{1,5})\s*(?:則|筆|篇|個)"
)
_PUNCTUATION_PATTERN = re.compile(
    r"[\s\u3000，。！？!?、：:；;「」『』（）()【】\[\]<>〈〉…]+"
)


class UnitQueryIntent(StrEnum):
    HOMEPAGE = "homepage"
    ANNOUNCEMENT = "announcement"
    DOCUMENT = "document"


def classify_unit_query(text: str) -> UnitQueryIntent:
    if any(term in text for term in HOMEPAGE_INTENT_TERMS):
        return UnitQueryIntent.HOMEPAGE
    if any(term in text for term in ANNOUNCEMENT_INTENT_TERMS):
        return UnitQueryIntent.ANNOUNCEMENT
    return UnitQueryIntent.DOCUMENT


def extract_announcement_topic(
    text: str,
    directory: OfficialUnitDirectory,
) -> str | None:
    residual = text
    matches = sorted(
        directory.aliases,
        key=lambda value: (-len(value), value),
    )
    for alias in matches:
        if alias in residual:
            residual = residual.replace(alias, " ")
    residual = _COUNT_PATTERN.sub(" ", residual)
    for phrase in sorted(
        (*ANNOUNCEMENT_INTENT_TERMS, *_OPERATION_TERMS),
        key=lambda value: (-len(value), value),
    ):
        residual = residual.replace(phrase, " ")
    residual = re.sub(r"(?:最近|最新|近期|一般|的|一下)", " ", residual)
    residual = _PUNCTUATION_PATTERN.sub(" ", residual).strip()
    return residual or None
