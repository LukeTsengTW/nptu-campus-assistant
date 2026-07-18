from __future__ import annotations

import hashlib
import json
import re

from nptu_assistant.crawlers.official_units import (
    OfficialUnitDirectory,
    load_default_official_unit_directory,
)
from nptu_assistant.crawlers.unit_intents import (
    UnitQueryIntent,
    classify_unit_query,
    extract_announcement_topic,
)
from nptu_assistant.rag.models import (
    GeneratedAnswer,
    ModelTurn,
    ResponseKind,
    ToolCall,
)


MIN_DOCUMENT_RELEVANCE = 0.35
DEFAULT_ANNOUNCEMENT_LIMIT = 5
MAX_ANNOUNCEMENT_LIMIT = 20
ANNOUNCEMENT_COUNT_PATTERN = re.compile(
    r"(?P<count>\d{1,3}|[零一二三四五六七八九十百兩]{1,5})\s*(?:則|筆|篇)"
)
CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "兩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_DOCUMENT_OPERATION_PREFIX = re.compile(
    r"^(?:請問|麻煩|幫我|幫忙|我想|想要|請)?(?:查詢|搜尋|搜索|查|找|看)?"
)
_FOLLOW_UP_PREFIXES = ("那", "那個", "剛才", "剛剛", "前面", "這個", "它")
_FOLLOW_UP_REFERENCE = re.compile(r"^(?:(?:那個|這個|剛才|剛剛|前面)(?:的)?|那|它)\s*")


def _is_relevant_result(result: dict[str, object]) -> bool:
    if result.get("kind") == "announcement":
        return True
    value = result.get("score", 0.0)
    if not isinstance(value, (str, int, float)):
        return False
    try:
        return float(value) >= MIN_DOCUMENT_RELEVANCE
    except (TypeError, ValueError):
        return False


def _parse_announcement_count(value: str) -> int | None:
    if value.isdecimal():
        return int(value)
    remaining = value
    total = 0
    if "百" in remaining:
        hundreds, remaining = remaining.split("百", 1)
        digit = CHINESE_DIGITS.get(hundreds or "一")
        if digit is None:
            return None
        total += digit * 100
    if "十" in remaining:
        tens, remaining = remaining.split("十", 1)
        digit = CHINESE_DIGITS.get(tens or "一")
        if digit is None:
            return None
        total += digit * 10
    if remaining:
        digit = CHINESE_DIGITS.get(remaining)
        if digit is None:
            return None
        total += digit
    return total


def _announcement_limit(question: str) -> tuple[int, bool]:
    match = ANNOUNCEMENT_COUNT_PATTERN.search(question)
    if match is None:
        return DEFAULT_ANNOUNCEMENT_LIMIT, False
    requested = _parse_announcement_count(match.group("count"))
    if requested is None:
        return DEFAULT_ANNOUNCEMENT_LIMIT, False
    return max(
        1, min(requested, MAX_ANNOUNCEMENT_LIMIT)
    ), requested > MAX_ANNOUNCEMENT_LIMIT


def _clean_document_question(question: str) -> str:
    cleaned = _DOCUMENT_OPERATION_PREFIX.sub("", question.strip())
    return re.sub(r"[\s，。！？、；：]+", " ", cleaned).strip()


def _fake_document_plan(
    input_items: list[dict[str, object]],
    question: str,
) -> dict[str, object]:
    user_questions = [
        str(item.get("content", ""))
        for item in input_items
        if item.get("role") == "user" and item.get("content")
    ]
    current = _clean_document_question(question)
    previous = (
        _clean_document_question(user_questions[-2]) if len(user_questions) > 1 else ""
    )
    is_follow_up = len(current) <= 16 or current.startswith(_FOLLOW_UP_PREFIXES)
    if is_follow_up:
        current = _FOLLOW_UP_REFERENCE.sub("", current).strip()
    standalone = " ".join(
        value for value in (previous if is_follow_up else "", current) if value
    )
    concepts = list(
        dict.fromkeys(value for value in standalone.split() if value.strip())
    )[:8]
    concepts = concepts or [standalone]
    variants = list(dict.fromkeys((standalone, *concepts)))
    return {
        "query": standalone,
        "search_queries": variants[:4],
        "concepts": concepts[:8],
        "limit": 6,
    }


class FakeEmbeddingProvider:
    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions

    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append(
                [
                    ((digest[index % len(digest)] / 255.0) * 2.0) - 1.0
                    for index in range(self.dimensions)
                ]
            )
        return vectors


class FakeLlmProvider:
    def __init__(self, official_units: OfficialUnitDirectory | None = None) -> None:
        self._official_units = official_units or load_default_official_unit_directory()

    def create_turn(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> ModelTurn:
        del instructions, tools
        question = next(
            (
                str(item.get("content", ""))
                for item in reversed(input_items)
                if item.get("role") == "user"
            ),
            "",
        )
        _, count_was_limited = _announcement_limit(question)
        count_limit_notice = (
            "單次查詢上限為 20 則，已依上限查詢。"
            if count_was_limited
            and classify_unit_query(question) is UnitQueryIntent.ANNOUNCEMENT
            else None
        )
        outputs = [
            item for item in input_items if item.get("type") == "function_call_output"
        ]
        if outputs:
            results: list[dict[str, object]] = []
            unit_error: dict[str, object] | None = None
            for item in outputs:
                try:
                    payload = json.loads(str(item.get("output", "{}")))
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if isinstance(payload.get("error"), dict):
                    unit_error = payload["error"]
                    break
                if isinstance(payload.get("results"), list):
                    results.extend(
                        value for value in payload["results"] if isinstance(value, dict)
                    )
            results = [result for result in results if _is_relevant_result(result)]
            if unit_error is not None:
                code = str(unit_error.get("code", ""))
                generated = GeneratedAnswer(
                    answer=str(unit_error.get("message", "目前無法完成查詢。")),
                    used_source_ids=[],
                    response_kind=(
                        ResponseKind.CLARIFICATION
                        if code in {"unknown_unit", "ambiguous_unit"}
                        else ResponseKind.INSUFFICIENT
                    ),
                )
            elif not results:
                generated = GeneratedAnswer(
                    answer="\n".join(
                        value
                        for value in ("目前查不到符合條件的資料。", count_limit_notice)
                        if value is not None
                    ),
                    used_source_ids=[],
                    response_kind=ResponseKind.INSUFFICIENT,
                )
            else:
                lines: list[str] = []
                used_source_ids: list[str] = []
                announcement_units: list[str] = []
                for result in results:
                    source_id = str(result.get("id", "")).strip()
                    if source_id:
                        used_source_ids.append(source_id)
                    title = str(result.get("title", "來源"))
                    url = str(result.get("url", "")).strip()
                    if result.get("kind") == "announcement":
                        published_at = str(result.get("published_at") or "日期未提供")
                        if url:
                            lines.append(f"[{published_at}｜{title}]({url})")
                        else:
                            lines.append(f"{published_at}｜{title}")
                        announcement_unit = str(result.get("unit", "")).strip()
                        if (
                            announcement_unit
                            and announcement_unit not in announcement_units
                        ):
                            announcement_units.append(announcement_unit)
                    else:
                        lines.append(f"根據「{title}」：{result.get('content', '')}")
                        if url:
                            lines.append(url)
                if announcement_units:
                    lines.append(f"資料來源：{'、'.join(announcement_units)}官方網站")
                if count_limit_notice:
                    lines.append(count_limit_notice)
                generated = GeneratedAnswer(
                    answer="\n".join(lines),
                    used_source_ids=used_source_ids,
                    response_kind=ResponseKind.GROUNDED,
                )
            return ModelTurn(
                output_items=[{"type": "message", "role": "assistant"}],
                generated=generated,
            )

        classified_intent = classify_unit_query(question)
        announcement_intent = classified_intent is UnitQueryIntent.ANNOUNCEMENT
        document_intent = any(
            term in question
            for term in (
                "介紹",
                "業務",
                "職掌",
                "校規",
                "申請",
                "流程",
                "規定",
                "學貸",
                "學分",
                "課程",
            )
        )
        explicit_both = bool(
            len(question) <= 50
            and re.search(
                r"(?:公告.*(?:與|和|及|以及).*(?:流程|規定|申請|文件)|"
                r"(?:流程|規定|申請|文件).*(?:與|和|及|以及).*公告)",
                question,
            )
        )
        if announcement_intent and document_intent and not explicit_both:
            if "為準" in question and "正式文件" in question:
                announcement_intent = False
            else:
                document_intent = False
        matching_aliases = [
            alias
            for alias in sorted(
                self._official_units.aliases,
                key=lambda value: (-len(value), value),
            )
            if alias in question
        ]
        unit = matching_aliases[0] if matching_aliases else None
        if unit is None:
            unit_text = re.sub(
                r"(?:請問|麻煩|幫我|幫忙|我想|想要|請)?(?:查詢|查|看|找|知道)?",
                "",
                question,
                count=1,
            )
            unit_match = re.search(
                r"[\u4e00-\u9fff]{2,12}?(?:學院|學系|學程|中心|處|組|室|系)",
                unit_text,
            )
            unit = unit_match.group(0) if unit_match else None
        announcement_query = (
            extract_announcement_topic(question, self._official_units)
            if announcement_intent
            else None
        )
        if (
            announcement_intent
            and unit is None
            and any(term in question for term in ("獎學金", "獎助學金"))
        ):
            announcement_query = question
        announcement_limit, _ = _announcement_limit(question)
        announcement_arguments = json.dumps(
            {
                "query": announcement_query,
                "limit": announcement_limit,
                "sort": "newest",
                "unit": unit,
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        )
        document_arguments = json.dumps(
            _fake_document_plan(input_items, question),
            ensure_ascii=False,
        )
        calls: list[ToolCall] = []
        if announcement_intent:
            calls.append(
                ToolCall(
                    "fake-announcements", "search_announcements", announcement_arguments
                )
            )
        if document_intent or not announcement_intent:
            calls.append(
                ToolCall("fake-documents", "search_documents", document_arguments)
            )
        return ModelTurn(
            output_items=[
                {
                    "type": "function_call",
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                }
                for call in calls
            ],
            tool_calls=calls,
        )
