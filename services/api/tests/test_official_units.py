from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest
import yaml

from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.official_units import (
    AnnouncementStrategy,
    DocumentSearchScope,
    OfficialUnitDirectory,
    UnitStatus,
    load_official_unit_directory,
    load_official_unit_directory_for_config,
)
from nptu_assistant.crawlers.resolution import UnitResolutionStatus, UnitSourceResolver
from nptu_assistant.crawlers.site_models import SearchDeadline, SearchPlan
from nptu_assistant.crawlers.site_search import ScopedAnnouncementIngestionResult
from nptu_assistant.crawlers.site_search import (
    SITE_SEARCH_FAILURE_WARNING,
    SITE_SEARCH_PARTIAL_WARNING,
)
from nptu_assistant.crawlers.unit_intents import (
    UnitQueryIntent,
    classify_unit_query,
    extract_announcement_topic,
)
from nptu_assistant.providers.fake import FakeLlmProvider
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.tools import ToolExecutor


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
ANNOUNCEMENT_CONFIG = WORKSPACE_ROOT / "data/sources/announcements.yaml"
UNIT_CONFIG = WORKSPACE_ROOT / "data/sources/official_units.yaml"


def project_directory() -> OfficialUnitDirectory:
    return load_official_unit_directory(UNIT_CONFIG)


def test_missing_sibling_directory_uses_project_registry(tmp_path: Path) -> None:
    directory = load_official_unit_directory_for_config(tmp_path / "announcements.yaml")

    assert directory.get("資訊學院").homepage_url == "https://ccs.nptu.edu.tw/"


def test_registry_rejects_explicit_alias_colliding_with_derived_alias(
    tmp_path: Path,
) -> None:
    payload = yaml.safe_load(UNIT_CONFIG.read_text(encoding="utf-8"))
    payload["units"][0]["aliases"].append("資工系")
    config = tmp_path / "official_units.yaml"
    config.write_text(
        yaml.safe_dump(payload, allow_unicode=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="alias 不可對應多個單位"):
        load_official_unit_directory(config)


def project_resolver() -> UnitSourceResolver:
    keyword = load_keyword_search_config(ANNOUNCEMENT_CONFIG)
    return UnitSourceResolver(
        load_source_configs(ANNOUNCEMENT_CONFIG),
        keyword.aliases,
        keyword.source_routes,
        project_directory(),
    )


class RecordingRetriever:
    def __init__(self, announcements: list[Evidence] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.announcements = announcements or []

    def search_announcements(self, **kwargs: object) -> list[Evidence]:
        self.calls.append(("search_announcements", kwargs))
        return list(self.announcements)

    def search_documents_with_plan(self, **kwargs: object) -> list[Evidence]:
        self.calls.append(("search_documents_with_plan", kwargs))
        return []

    def search_documents(self, **kwargs: object) -> list[Evidence]:
        self.calls.append(("search_documents", kwargs))
        return []

    def get_announcement(self, announcement_id: str) -> Evidence | None:
        del announcement_id
        return None


class ScopedAnnouncementIngestor:
    def __init__(
        self,
        canonical_urls: tuple[str, ...],
        warning: str | None = None,
    ) -> None:
        self.canonical_urls = canonical_urls
        self.warning = warning
        self.scopes: list[DocumentSearchScope] = []
        self.sorts: list[object] = []
        self.topics: list[str | None] = []

    def new_deadline(self) -> SearchDeadline:
        return SearchDeadline.after(10)

    def search_unit_announcements(
        self,
        plan: SearchPlan,
        *,
        scope: DocumentSearchScope,
        max_items: int,
        deadline: SearchDeadline,
        sort: object = "newest",
        topic: str | None = None,
    ) -> ScopedAnnouncementIngestionResult:
        del plan, max_items, deadline
        self.scopes.append(scope)
        self.sorts.append(sort)
        self.topics.append(topic)
        return ScopedAnnouncementIngestionResult(
            self.canonical_urls,
            self.warning,
            persisted_count=len(self.canonical_urls),
        )


def document_arguments(query: str) -> str:
    return json.dumps(
        {
            "query": query,
            "search_queries": [query],
            "concepts": [query],
            "limit": 6,
        },
        ensure_ascii=False,
    )


def announcement_arguments(
    query: str | None,
    unit: str | None,
    *,
    sort: str = "newest",
) -> str:
    return json.dumps(
        {
            "query": query,
            "limit": 5,
            "sort": sort,
            "unit": unit,
            "date_from": None,
            "date_to": None,
        },
        ensure_ascii=False,
    )


def test_full_registry_is_valid_and_complete() -> None:
    directory = project_directory()
    sources = {source.name for source in load_source_configs(ANNOUNCEMENT_CONFIG)}

    assert len(directory.units) == 66
    assert len(directory.active_units) == 64
    assert sum(unit.status is UnitStatus.DISCONTINUED for unit in directory.units) == 2
    assert all(
        unit.homepage_url or unit.unsupported_reason for unit in directory.active_units
    )
    assert all(
        unit.announcement_source_name in sources
        for unit in directory.active_units
        if unit.announcement_source_name
    )
    assert (
        sum(
            unit.announcement_strategy is AnnouncementStrategy.CONFIGURED_LISTING
            for unit in directory.units
        )
        == 1
    )
    assert (
        sum(
            unit.announcement_strategy is AnnouncementStrategy.SCOPED_SITE_SEARCH
            for unit in directory.units
        )
        == 63
    )


def test_every_alias_resolves_deterministically() -> None:
    directory = project_directory()
    resolver = project_resolver()

    for alias, canonical in directory.aliases.items():
        result = resolver.resolve(alias, None)
        assert result.canonical_unit == canonical, alias
        assert result.status not in {
            UnitResolutionStatus.UNKNOWN,
            UnitResolutionStatus.AMBIGUOUS,
        }, alias


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("電科系", "電腦科學與人工智慧學系"),
        ("資工系", "資訊工程學系"),
        ("企管系", "企業管理學系"),
        ("教育系", "教育學系"),
        ("中文系", "中國語文學系"),
        ("應化系", "應用化學系"),
        ("體育系", "體育學系"),
    ],
)
def test_representative_homepage_is_config_backed(alias: str, canonical: str) -> None:
    retriever = RecordingRetriever()
    result = ToolExecutor(retriever, unit_resolver=project_resolver()).execute(
        "search_documents",
        document_arguments(f"{alias}官方網站"),
    )

    unit = project_directory().get(canonical)
    assert unit is not None
    assert result.warning is None
    assert result.evidence[0].url == unit.homepage_url
    assert result.evidence[0].unit == canonical
    assert result.evidence[0].score == 1.0
    assert retriever.calls == []


@pytest.mark.parametrize(
    "question",
    [
        "資工系最新公告",
        "資工系最新消息",
        "查資工系的最新資訊",
        "資工系近期動態",
    ],
)
def test_generic_latest_intent_uses_null_topic(question: str) -> None:
    turn = FakeLlmProvider(project_directory()).create_turn(
        instructions="",
        input_items=[{"role": "user", "content": question}],
        tools=[],
    )

    assert turn.tool_calls is not None
    assert [call.name for call in turn.tool_calls] == ["search_announcements"]
    arguments = json.loads(turn.tool_calls[0].arguments)
    assert arguments["query"] is None
    assert arguments["unit"] == "資工系"
    assert arguments["sort"] == "newest"
    assert arguments["limit"] == 5


@pytest.mark.parametrize(
    ("question", "topic"),
    [
        ("資工系人工智慧演講最新公告", "人工智慧演講"),
        ("企管系實習最新資訊", "實習"),
        ("教育系招生說明會公告", "招生說明會"),
    ],
)
def test_topic_announcement_preserves_real_topic(question: str, topic: str) -> None:
    directory = project_directory()

    assert classify_unit_query(question) is UnitQueryIntent.ANNOUNCEMENT
    assert extract_announcement_topic(question, directory) == topic
    turn = FakeLlmProvider(directory).create_turn(
        instructions="",
        input_items=[{"role": "user", "content": question}],
        tools=[],
    )
    assert turn.tool_calls is not None
    assert json.loads(turn.tool_calls[0].arguments)["query"] == topic


@pytest.mark.parametrize(
    "question",
    ["資工系畢業門檻", "資訊安全課程規定", "企管系實習辦法", "資工系招生資訊"],
)
def test_document_intent_does_not_misroute_information_term(question: str) -> None:
    turn = FakeLlmProvider(project_directory()).create_turn(
        instructions="",
        input_items=[{"role": "user", "content": question}],
        tools=[],
    )

    assert classify_unit_query(question) is UnitQueryIntent.DOCUMENT
    assert turn.tool_calls is not None
    assert [call.name for call in turn.tool_calls] == ["search_documents"]


def test_scoped_announcement_search_stays_on_unit_host() -> None:
    official_url = "https://csie.nptu.edu.tw/p/406-1009-200001.php"
    scoped = ScopedAnnouncementIngestor((official_url,))
    persisted = Evidence(
        id="7dff5883-9e4d-4a8a-95ba-ceb79ef8e9dc",
        kind=AnswerType.ANNOUNCEMENT,
        title="資訊工程學系最新公告",
        url=official_url,
        unit="資訊工程學系",
        published_at=date(2026, 7, 18),
        content="公告內容",
        score=0.9,
    )
    retriever = RecordingRetriever([persisted])

    result = ToolExecutor(
        retriever,
        unit_resolver=project_resolver(),
        site_page_ingestor=scoped,
    ).execute(
        "search_announcements",
        announcement_arguments("資工系最新公告", "資工系"),
    )

    assert result.warning is None
    assert [item.url for item in result.evidence] == [official_url]
    assert result.evidence[0].kind is AnswerType.ANNOUNCEMENT
    assert result.evidence[0].id == persisted.id
    assert scoped.scopes[0].allowed_hosts == ("csie.nptu.edu.tw",)
    assert retriever.calls[0][1]["canonical_urls"] == (official_url,)


def test_scoped_cache_rejects_other_unit_results() -> None:
    contamination = Evidence(
        id="other-unit",
        kind=AnswerType.ANNOUNCEMENT,
        title="其他單位公告",
        url="https://mis.nptu.edu.tw/p/406-1000-1.php",
        unit="資訊管理學系",
        published_at=date(2026, 7, 18),
        content="其他單位",
        score=0.9,
    )
    retriever = RecordingRetriever([contamination])
    scoped = ScopedAnnouncementIngestor(())

    result = ToolExecutor(
        retriever,
        unit_resolver=project_resolver(),
        site_page_ingestor=scoped,
    ).execute(
        "search_announcements",
        announcement_arguments(None, "資工系"),
    )

    assert result.evidence == []
    assert result.warning is None


def test_scoped_failure_uses_matching_cache_with_failure_warning() -> None:
    cached = Evidence(
        id="persisted-cache-id",
        kind=AnswerType.ANNOUNCEMENT,
        title="資訊工程學系快取公告",
        url="https://csie.nptu.edu.tw/p/406-cache.php",
        unit="資訊工程學系",
        published_at=date(2026, 7, 17),
        content="快取內容",
        score=0.8,
    )
    result = ToolExecutor(
        RecordingRetriever([cached]),
        unit_resolver=project_resolver(),
        site_page_ingestor=ScopedAnnouncementIngestor((), SITE_SEARCH_FAILURE_WARNING),
    ).execute(
        "search_announcements",
        announcement_arguments(None, "資工系"),
    )

    assert result.evidence == [cached]
    assert result.warning == SITE_SEARCH_FAILURE_WARNING


def test_scoped_partial_persistence_returns_only_database_backed_urls() -> None:
    persisted_url = "https://csie.nptu.edu.tw/p/406-persisted.php"
    persisted = Evidence(
        id="database-announcement-id",
        kind=AnswerType.ANNOUNCEMENT,
        title="已持久化公告",
        url=persisted_url,
        unit="資訊工程學系",
        published_at=date(2026, 7, 18),
        content="正式內容",
        score=0.9,
    )
    retriever = RecordingRetriever([persisted])
    scoped = ScopedAnnouncementIngestor((persisted_url,), SITE_SEARCH_PARTIAL_WARNING)
    result = ToolExecutor(
        retriever,
        unit_resolver=project_resolver(),
        site_page_ingestor=scoped,
    ).execute(
        "search_announcements",
        announcement_arguments("人工智慧", "資工系", sort="relevance"),
    )

    assert result.evidence == [persisted]
    assert result.warning == SITE_SEARCH_PARTIAL_WARNING
    assert retriever.calls[0][1]["canonical_urls"] == (persisted_url,)
    assert retriever.calls[0][1]["sort"].value == "relevance"
    assert scoped.sorts[0].value == "relevance"
    assert scoped.topics == ["人工智慧"]
