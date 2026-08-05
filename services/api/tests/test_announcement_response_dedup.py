from __future__ import annotations

from datetime import date

import pytest

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.rag.announcement_dedup import (
    DeduplicatingSqlRetriever,
    deduplicate_announcement_evidence,
)
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import AnnouncementSort


def _evidence(
    *,
    identifier: str,
    title: str,
    url: str,
    published_at: date = date(2026, 8, 5),
    unit: str = "生活輔導組",
) -> Evidence:
    return Evidence(
        id=identifier,
        kind=AnswerType.ANNOUNCEMENT,
        title=title,
        url=url,
        unit=unit,
        published_at=published_at,
        content=title,
        score=0.65,
    )


def test_response_dedup_keeps_first_same_title_date_and_unit() -> None:
    first = _evidence(
        identifier="first",
        title="【獎助學金】ＡＢＣ　獎學金",
        url="https://staf-life.nptu.edu.tw/p/406-1074-200010.php?Lang=zh-tw",
    )
    duplicate = _evidence(
        identifier="duplicate",
        title="【獎助學金】ABC 獎學金",
        url="https://staf-life.nptu.edu.tw/p/406-1074-200011.php?Lang=zh-tw",
    )

    result = deduplicate_announcement_evidence([first, duplicate], limit=5)

    assert result == [first]


def test_response_dedup_keeps_same_title_for_different_date_or_unit() -> None:
    first = _evidence(
        identifier="first",
        title="例行公告",
        url="https://www.nptu.edu.tw/p/406-1000-200020.php",
    )
    different_date = _evidence(
        identifier="different-date",
        title="例行公告",
        url="https://www.nptu.edu.tw/p/406-1000-200021.php",
        published_at=date(2026, 8, 4),
    )
    different_unit = _evidence(
        identifier="different-unit",
        title="例行公告",
        url="https://www.nptu.edu.tw/p/406-1000-200022.php",
        unit="教務處",
    )

    result = deduplicate_announcement_evidence(
        [first, different_date, different_unit],
        limit=5,
    )

    assert result == [first, different_date, different_unit]


def test_response_dedup_also_collapses_equivalent_nptu_page_routes() -> None:
    route_406 = _evidence(
        identifier="route-406",
        title="原始標題",
        url="https://staf-life.nptu.edu.tw/p/406-1074-198126,r3893.php?Lang=zh-tw",
    )
    route_404 = _evidence(
        identifier="route-404",
        title="略有差異的標題",
        url="https://staf-life.nptu.edu.tw/p/404-1074-198126.php?Lang=zh-tw",
    )

    result = deduplicate_announcement_evidence([route_406, route_404], limit=5)

    assert result == [route_406]


def test_deduplicating_retriever_overfetches_before_applying_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _evidence(
            identifier="duplicate-1",
            title="重複公告",
            url="https://www.nptu.edu.tw/p/406-1000-200030.php",
        ),
        _evidence(
            identifier="duplicate-2",
            title="重複公告",
            url="https://www.nptu.edu.tw/p/406-1000-200031.php",
        ),
        *[
            _evidence(
                identifier=f"unique-{index}",
                title=f"不同公告 {index}",
                url=f"https://www.nptu.edu.tw/p/406-1000-{200040 + index}.php",
            )
            for index in range(5)
        ],
    ]
    observed: dict[str, int] = {}

    def fake_search_announcements(
        self: SqlRetriever,
        **kwargs: object,
    ) -> list[Evidence]:
        del self
        observed["limit"] = int(kwargs["limit"])
        return rows[: observed["limit"]]

    monkeypatch.setattr(SqlRetriever, "search_announcements", fake_search_announcements)
    retriever = object.__new__(DeduplicatingSqlRetriever)

    result = retriever.search_announcements(
        query=None,
        limit=5,
        sort=AnnouncementSort.NEWEST,
    )

    assert observed["limit"] == 15
    assert [item.id for item in result] == [
        "duplicate-1",
        "unique-0",
        "unique-1",
        "unique-2",
        "unique-3",
    ]
