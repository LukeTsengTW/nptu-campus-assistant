from __future__ import annotations

from typing import Protocol

from nptu_assistant.crawlers.models import AnnouncementCandidate


class CrawlerAdapter(Protocol):
    def parse_listing(self, content: str) -> list[AnnouncementCandidate]: ...

    def parse_detail(self, content: str) -> str: ...
