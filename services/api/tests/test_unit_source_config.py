from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def html_source_payload() -> dict[str, object]:
    return {
        "name": "information-college-html",
        "adapter": "nptu_html_list",
        "url": "https://ccs.nptu.edu.tw/p/403-1025-1019-1.php?Lang=zh-tw",
        "unit": "資訊學院",
        "aliases": ["資訊學院"],
        "category": "學術單位公告",
        "enabled": True,
        "crawl_interval_minutes": 60,
        "max_items": 20,
        "allowed_hosts": ["ccs.nptu.edu.tw"],
        "selectors": {
            "listing": "section.mb",
            "item": ".row.listBS",
            "date": "i.mdate",
            "title_link": ".mtitle > a[href]",
            "link_attribute": "href",
        },
        "detail": {"enabled": False},
    }


def test_information_college_source_loads_from_project_config() -> None:
    configs = load_source_configs(WORKSPACE_ROOT / "data/sources/announcements.yaml")
    source = next(item for item in configs if item.name == "information-college-html")

    assert source.unit == "資訊學院"
    assert source.aliases == ["資訊學院"]
    assert source.allowed_hosts == ["ccs.nptu.edu.tw"]
    assert source.url == "https://ccs.nptu.edu.tw/p/403-1025-1019-1.php?Lang=zh-tw"
    assert source.max_items == 20
    assert source.selectors is not None
    assert source.selectors.listing == "section.mb"
    assert source.selectors.item == ".row.listBS"
    assert source.selectors.date == "i.mdate"
    assert source.selectors.title_link == ".mtitle > a[href]"
    assert source.selectors.link_attribute == "href"
    assert source.detail is not None
    assert source.detail.enabled is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda item: item.update(url="http://ccs.nptu.edu.tw/p/403-1025-1019-1.php"), "HTTPS"),
        (lambda item: item.update(allowed_hosts=["example.com"]), "host"),
        (lambda item: item.update(allowed_hosts=["www.nptu.edu.tw"]), "allowlist"),
        (lambda item: item.update(aliases=[""]), "別名"),
        (lambda item: item.update(aliases=["資訊學院", "資訊學院"]), "重複"),
        (lambda item: item.update(selectors=None), "selectors"),
        (
            lambda item: item["selectors"].update(listing="section["),  # type: ignore[union-attr]
            "selector",
        ),
        (lambda item: item.update(unexpected=True), "Extra inputs"),
    ],
)
def test_invalid_html_source_config_is_rejected(mutation, message: str) -> None:
    payload = deepcopy(html_source_payload())
    mutation(payload)

    with pytest.raises(ValidationError, match=message):
        CrawlerSourceConfig.model_validate(payload)


def test_duplicate_source_names_are_rejected(tmp_path: Path) -> None:
    source = html_source_payload()
    path = tmp_path / "sources.yaml"
    path.write_text(
        yaml.safe_dump({"sources": [source, deepcopy(source)]}, allow_unicode=True),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="不可重複"):
        load_source_configs(path)


def test_existing_overview_and_fixture_configs_remain_valid() -> None:
    overview = CrawlerSourceConfig.model_validate(
        {
            "name": "nptu-overview",
            "adapter": "nptu_overview",
            "url": "https://www.nptu.edu.tw/feed.xml",
            "unit": "國立屏東大學",
        }
    )
    fixture = CrawlerSourceConfig.model_validate(
        {
            "name": "fixture",
            "adapter": "fixture",
            "url": "data/fixtures/announcements/overview.xml",
            "unit": "測試單位",
        }
    )

    assert overview.selectors is None
    assert fixture.selectors is None
