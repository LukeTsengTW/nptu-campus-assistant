from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class AnnouncementCandidate:
    title: str
    canonical_url: str
    unit: str
    category: str | None
    published_at: date
    deadline_at: date | None
    body: str
    warning: str | None = None
