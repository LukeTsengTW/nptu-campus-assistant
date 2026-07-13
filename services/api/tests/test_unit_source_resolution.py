from __future__ import annotations

from nptu_assistant.crawlers.aliases import AliasNormalizer
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_keyword_search_config, load_source_configs
from nptu_assistant.crawlers.resolution import (
    UnitResolutionStatus,
    UnitSourceResolver,
)

from pathlib import Path


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"


def project_resolver() -> UnitSourceResolver:
    return UnitSourceResolver(
        load_source_configs(CONFIG_PATH),
        load_keyword_search_config(CONFIG_PATH).aliases,
    )


def html_source(
    *,
    name: str,
    unit: str,
    alias: str,
    host: str,
    enabled: bool = True,
) -> CrawlerSourceConfig:
    return CrawlerSourceConfig.model_validate(
        {
            "name": name,
            "adapter": "nptu_html_list",
            "url": f"https://{host}/index.php",
            "unit": unit,
            "aliases": [alias],
            "category": "單位公告",
            "enabled": enabled,
            "allowed_hosts": [host],
            "selectors": {
                "listing": "section.mb",
                "item": ".row",
                "date": ".date",
                "title_link": "a[href]",
            },
        }
    )


def test_alias_normalizer_uses_longest_non_overlapping_alias() -> None:
    resolver = AliasNormalizer({"電科": "錯誤單位", "電科系": "電腦科學與人工智慧學系"})

    assert resolver.normalize("電科系最新公告") == "電腦科學與人工智慧學系最新公告"


def test_resolver_resolves_canonical_unit_and_query_mention() -> None:
    resolver = project_resolver()

    direct = resolver.resolve("資訊學院", "最新公告")
    from_query = resolver.resolve(None, "幫我查資訊學院的最新公告")

    for result in (direct, from_query):
        assert result.status is UnitResolutionStatus.RESOLVED
        assert result.canonical_unit == "資訊學院"
        assert result.source is not None
        assert result.source.name == "information-college-html"


def test_known_unit_without_enabled_source_is_unsupported() -> None:
    result = project_resolver().resolve("研發處", "最新公告")

    assert result.status is UnitResolutionStatus.UNSUPPORTED
    assert result.canonical_unit == "研究發展處"
    assert result.source is None


def test_unknown_explicit_or_query_unit_requires_clarification() -> None:
    resolver = project_resolver()

    assert resolver.resolve("火星學院", "最新公告").status is UnitResolutionStatus.UNKNOWN
    query_result = resolver.resolve(None, "火星學院最新公告")
    assert query_result.status is UnitResolutionStatus.UNKNOWN
    assert "火星學院" in query_result.requested


def test_no_unit_keeps_general_announcement_flow() -> None:
    result = project_resolver().resolve(None, "最新公告")

    assert result.status is UnitResolutionStatus.NONE
    assert result.source is None


def test_duplicate_source_alias_is_ambiguous_with_stable_candidates() -> None:
    sources = [
        html_source(name="beta", unit="乙中心", alias="共同中心", host="b.nptu.edu.tw"),
        html_source(name="alpha", unit="甲中心", alias="共同中心", host="a.nptu.edu.tw"),
    ]
    resolver = UnitSourceResolver(sources, {})

    result = resolver.resolve("共同中心", "最新公告")

    assert result.status is UnitResolutionStatus.AMBIGUOUS
    assert result.candidates == ("乙中心", "甲中心")
    assert result.source is None


def test_unit_and_query_pointing_to_different_units_is_ambiguous() -> None:
    result = project_resolver().resolve("資訊學院", "研發處最新公告")

    assert result.status is UnitResolutionStatus.AMBIGUOUS
    assert result.candidates == ("研究發展處", "資訊學院")


def test_known_and_unknown_units_in_the_same_query_are_not_silently_narrowed() -> None:
    result = project_resolver().resolve(None, "資訊學院和火星學院最新公告")

    assert result.status is UnitResolutionStatus.AMBIGUOUS
    assert result.candidates == ("火星學院", "資訊學院")


def test_disabled_source_is_known_but_unsupported() -> None:
    source = html_source(
        name="disabled",
        unit="測試中心",
        alias="測試中心",
        host="test.nptu.edu.tw",
        enabled=False,
    )

    result = UnitSourceResolver([source], {}).resolve("測試中心", "最新公告")

    assert result.status is UnitResolutionStatus.UNSUPPORTED
    assert result.canonical_unit == "測試中心"
