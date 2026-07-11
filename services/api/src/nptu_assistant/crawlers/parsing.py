from __future__ import annotations

import re
from datetime import date, datetime


_DATE = re.compile(r"(?P<year>\d{3,4})[年\-/\.](?P<month>\d{1,2})[月\-/\.](?P<day>\d{1,2})日?")
_DEADLINE = re.compile(r"(?:截止(?:日|日期|時間)?|報名期限)[：:\s]*(?P<date>\d{3,4}[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?)")


def _build_date(year: int, month: int, day: int) -> date:
    if year < 1911:
        year += 1911
    return date(year, month, day)


def parse_published_at(value: str) -> date:
    normalized = value.strip()
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        match = _DATE.search(normalized)
        if not match:
            raise ValueError(f"無法解析發布日期：{value}") from None
        return _build_date(*(int(match.group(name)) for name in ("year", "month", "day")))


def parse_deadline(value: str) -> date | None:
    match = _DEADLINE.search(value)
    return parse_published_at(match.group("date")) if match else None
