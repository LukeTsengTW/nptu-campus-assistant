from __future__ import annotations

import xml.etree.ElementTree as ET

from nptu_assistant.core.security import is_allowed_nptu_url
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.parsing import parse_deadline, parse_published_at
from nptu_assistant.ingestion.cleaning import extract_clean_html


class NptuOverviewAdapter:
    category = "總覽"

    def parse_listing(self, content: str) -> list[AnnouncementCandidate]:
        root = ET.fromstring(content)
        candidates: list[AnnouncementCandidate] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            url = (item.findtext("link") or "").strip()
            if not title or not is_allowed_nptu_url(url):
                continue
            description = item.findtext("description") or ""
            body = extract_clean_html(description)
            try:
                candidates.append(
                    AnnouncementCandidate(
                        title=title,
                        canonical_url=url,
                        unit=(item.findtext("author") or "國立屏東大學").strip(),
                        category=self.category,
                        published_at=parse_published_at(item.findtext("pubDate") or ""),
                        deadline_at=parse_deadline(body),
                        body=body,
                    )
                )
            except (TypeError, ValueError):
                continue
        return candidates

    def parse_detail(self, content: str) -> str:
        return extract_clean_html(content)
