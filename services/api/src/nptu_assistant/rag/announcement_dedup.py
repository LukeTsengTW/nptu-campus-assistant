from __future__ import annotations

from datetime import date

from nptu_assistant.core.security import nptu_content_identity
from nptu_assistant.crawlers.announcement_identity import announcement_title_identity
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import AnnouncementSort


ANNOUNCEMENT_DEDUP_OVERFETCH_FACTOR = 3
ANNOUNCEMENT_MAX_LIMIT = 20


def deduplicate_announcement_evidence(
    evidence: list[Evidence],
    *,
    limit: int,
) -> list[Evidence]:
    """Keep the first ranked announcement for each URL or title fallback identity."""
    deduplicated: list[Evidence] = []
    seen_content_identities: set[str] = set()
    seen_title_identities: set[tuple[str, date | None, str]] = set()

    for item in evidence:
        try:
            content_identity = nptu_content_identity(item.url)
        except ValueError:
            content_identity = item.url
        title_identity = announcement_title_identity(
            title=item.title,
            published_at=item.published_at,
            unit=item.unit,
        )
        if (
            content_identity in seen_content_identities
            or title_identity in seen_title_identities
        ):
            continue
        seen_content_identities.add(content_identity)
        seen_title_identities.add(title_identity)
        deduplicated.append(item)
        if len(deduplicated) >= limit:
            break

    return deduplicated


class DeduplicatingSqlRetriever(SqlRetriever):
    """SQL retriever that removes duplicate announcements at the response boundary."""

    def search_announcements(
        self,
        *,
        query: str | None,
        limit: int,
        sort: AnnouncementSort,
        unit: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        canonical_urls: tuple[str, ...] | None = None,
    ) -> list[Evidence]:
        candidate_limit = min(
            ANNOUNCEMENT_MAX_LIMIT,
            max(limit, limit * ANNOUNCEMENT_DEDUP_OVERFETCH_FACTOR),
        )
        evidence = super().search_announcements(
            query=query,
            limit=candidate_limit,
            sort=sort,
            unit=unit,
            date_from=date_from,
            date_to=date_to,
            canonical_urls=canonical_urls,
        )
        return deduplicate_announcement_evidence(evidence, limit=limit)
