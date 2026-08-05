from __future__ import annotations

import unicodedata
from datetime import date


def normalize_announcement_text(value: str | None) -> str:
    """Normalize human-readable announcement text for conservative equality checks."""
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def announcement_title_identity(
    *,
    title: str,
    published_at: date,
    unit: str | None,
) -> tuple[str, date, str]:
    """Return a fallback identity for duplicate announcement listing entries."""
    return (
        normalize_announcement_text(title),
        published_at,
        normalize_announcement_text(unit),
    )
