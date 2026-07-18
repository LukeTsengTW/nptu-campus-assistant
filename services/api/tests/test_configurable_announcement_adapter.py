from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from bs4 import BeautifulSoup

from nptu_assistant.crawlers.adapters.factory import build_adapter
from nptu_assistant.crawlers.adapters.fixture import FixtureAdapter
from nptu_assistant.crawlers.adapters.nptu import NptuOverviewAdapter
from nptu_assistant.crawlers.adapters.nptu_html import NptuHtmlListAdapter
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.service import CrawlerService


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = WORKSPACE_ROOT / "data/sources/announcements.yaml"
FIXTURE_PATH = WORKSPACE_ROOT / "data/fixtures/announcements/nptu-ccs/listing.html"
OVERVIEW_FIXTURE_PATH = (
    WORKSPACE_ROOT / "data/fixtures/announcements/nptu-overview/listing.html"
)
SCHOLARSHIP_FIXTURE_PATH = (
    WORKSPACE_ROOT / "data/fixtures/announcements/nptu-scholarship/listing.html"
)


def information_college_config() -> CrawlerSourceConfig:
    return next(
        item
        for item in load_source_configs(CONFIG_PATH)
        if item.name == "information-college-html"
    )


def nptu_overview_config() -> CrawlerSourceConfig:
    return next(
        item
        for item in load_source_configs(CONFIG_PATH)
        if item.name == "nptu-overview"
    )


def scholarship_source_config(name: str) -> CrawlerSourceConfig:
    return next(item for item in load_source_configs(CONFIG_PATH) if item.name == name)


def test_nptu_overview_uses_official_html_listing_and_sorts_by_date() -> None:
    config = nptu_overview_config()
    items = NptuHtmlListAdapter(config).parse_listing(
        OVERVIEW_FIXTURE_PATH.read_text(encoding="utf-8")
    )

    assert config.url == "https://www.nptu.edu.tw/p/422-1000-1044.php?Lang=zh-tw"
    assert config.adapter == "nptu_html_list"
    assert [item.published_at for item in items] == [
        date(2026, 7, 15),
        date(2026, 7, 14),
    ]
    assert [item.title for item in items] == ["總覽最新公告", "總覽前一日公告"]
    assert all(item.unit == "國立屏東大學" for item in items)


def test_information_college_fixture_parses_six_sorted_announcements() -> None:
    items = NptuHtmlListAdapter(information_college_config()).parse_listing(
        FIXTURE_PATH.read_text(encoding="utf-8")
    )

    assert [item.published_at for item in items] == [
        date(2026, 7, 9),
        date(2026, 7, 9),
        date(2026, 7, 7),
        date(2026, 7, 7),
        date(2026, 7, 6),
        date(2026, 7, 2),
    ]
    assert [item.title for item in items] == [
        f"資訊學院測試公告{value}" for value in "一二三四五六"
    ]
    assert [item.canonical_url for item in items] == [
        "https://ccs.nptu.edu.tw/p/406-1025-197412,r1019.php?Lang=zh-tw",
        "https://ccs.nptu.edu.tw/p/406-1025-197411,r1019.php?Lang=zh-tw",
        "https://ccs.nptu.edu.tw/p/406-1025-197304,r1019.php?Lang=zh-tw",
        "https://ccs.nptu.edu.tw/p/406-1025-197298,r1019.php?Lang=zh-tw",
        "https://ccs.nptu.edu.tw/p/406-1025-197247,r1019.php?Lang=zh-tw",
        "https://ccs.nptu.edu.tw/p/406-1025-197201,r1019.php?Lang=zh-tw",
    ]
    assert all(item.unit == "資訊學院" for item in items)
    assert all(item.category == "學術單位公告" for item in items)


@pytest.mark.parametrize(
    ("source_name", "titles", "listing"),
    [
        (
            "student-scholarship-external-html",
            ["【獎助學金】外部基金會獎助學金公告", "【獎學金】校外公益獎學金申請公告"],
            "#cmb_1373_0",
        ),
        (
            "student-scholarship-internal-html",
            ["各項校內獎學金資訊一覽表"],
            "#cmb_1373_1",
        ),
    ],
)
def test_scholarship_fixture_parses_only_configured_tab(
    source_name: str,
    titles: list[str],
    listing: str,
) -> None:
    config = scholarship_source_config(source_name)
    items = NptuHtmlListAdapter(config).parse_listing(
        SCHOLARSHIP_FIXTURE_PATH.read_text(encoding="utf-8")
    )

    assert [item.title for item in items] == titles
    assert config.selectors is not None
    assert config.selectors.listing == listing
    assert all(item.unit == "生活輔導組" for item in items)
    assert all(item.category == config.category for item in items)
    assert all(
        item.canonical_url.startswith("https://staf-life.nptu.edu.tw/")
        for item in items
    )
    assert all("得獎名單" not in item.title for item in items)


def test_parser_skips_malformed_and_duplicate_items_without_leaving_listing_scope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    html = """
    <div class="row listBS"><i class="mdate">2030-01-01</i>
      <div class="mtitle"><a href="https://ccs.nptu.edu.tw/outside">列表外</a></div></div>
    <section class="mb">
      <div class="row listBS"><i class="mdate"> 2026-07-09 </i>
        <div class="mtitle"><a href="/valid#fragment">  有效\n公告  </a></div></div>
      <div class="row listBS"><i class="mdate">2026-07-09</i>
        <div class="mtitle"><a href="https://ccs.nptu.edu.tw/valid">重複</a></div></div>
      <div class="row listBS"><div class="mtitle"><a href="/missing-date">缺日期</a></div></div>
      <div class="row listBS"><i class="mdate">not-a-date</i>
        <div class="mtitle"><a href="/bad-date">日期錯誤</a></div></div>
      <div class="row listBS"><i class="mdate">2026-07-08</i>
        <div class="mtitle"><a>缺 href</a></div></div>
      <div class="row listBS"><i class="mdate">2026-07-08</i>
        <div class="mtitle"><a href="https://www.nptu.edu.tw/wrong-host">其他 NPTU host</a></div></div>
      <div class="row listBS"><i class="mdate">2026-07-08</i>
        <div class="mtitle"><a href="https://example.com/external">外站</a></div></div>
    </section>
    """

    with caplog.at_level(logging.WARNING):
        items = NptuHtmlListAdapter(information_college_config()).parse_listing(html)

    assert [(item.title, item.canonical_url) for item in items] == [
        ("有效 公告", "https://ccs.nptu.edu.tw/valid")
    ]
    assert (
        len(
            [
                record
                for record in caplog.records
                if record.message == "html_announcement_item_skipped"
            ]
        )
        == 5
    )


@pytest.mark.parametrize(
    "html",
    [
        "<html><body></body></html>",
        '<section class="mb"><p>沒有公告列</p></section>',
        '<section class="mb"><div class="row listBS"><i class="mdate">錯誤</i></div></section>',
    ],
)
def test_parser_rejects_missing_or_entirely_invalid_listing(html: str) -> None:
    with pytest.raises(ValueError, match="公告"):
        NptuHtmlListAdapter(information_college_config()).parse_listing(html)


def test_detail_parser_respects_configured_content_selector() -> None:
    payload = information_college_config().model_dump()
    payload["detail"] = {"enabled": True, "content_selector": ".meditor"}
    config = CrawlerSourceConfig.model_validate(payload)
    html = "<nav>導覽</nav><main><div class='meditor'><p>公告正文</p><script>bad()</script></div></main>"

    assert NptuHtmlListAdapter(config).parse_detail(html) == "公告正文"


def test_adapter_factory_preserves_existing_adapters() -> None:
    html = information_college_config()
    overview = CrawlerSourceConfig.model_validate(
        {
            "name": "overview",
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

    assert isinstance(build_adapter(html), NptuHtmlListAdapter)
    assert isinstance(build_adapter(overview), NptuOverviewAdapter)
    assert isinstance(build_adapter(fixture), FixtureAdapter)


class RecordingRepository:
    def __init__(self) -> None:
        self.candidates: list[AnnouncementCandidate] = []

    def upsert(self, candidate: AnnouncementCandidate, **_: object) -> str:
        self.candidates.append(candidate)
        return "created"

    def commit_source_refresh(
        self,
        candidates: list[AnnouncementCandidate],
        **_: object,
    ) -> list[str]:
        return [self.upsert(candidate) for candidate in candidates]


class BulkOnlyRepository(RecordingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.bulk_calls = 0

    def upsert(self, candidate: AnnouncementCandidate, **_: object) -> str:
        raise AssertionError(f"不應逐筆提交：{candidate.canonical_url}")

    def upsert_many(
        self,
        candidates: list[AnnouncementCandidate],
        **_: object,
    ) -> list[str]:
        self.bulk_calls += 1
        self.candidates.extend(candidates)
        return ["created"] * len(candidates)

    def commit_source_refresh(
        self,
        candidates: list[AnnouncementCandidate],
        **values: object,
    ) -> list[str]:
        return self.upsert_many(candidates, **values)


class AtomicSnapshotRepository(RecordingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.commit_calls: list[dict[str, object]] = []

    def upsert(self, candidate: AnnouncementCandidate, **_: object) -> str:
        raise AssertionError(f"不得在來源快照交易外逐筆提交：{candidate.canonical_url}")

    def commit_source_refresh(
        self,
        candidates: list[AnnouncementCandidate],
        **values: object,
    ) -> list[str]:
        assert isinstance(values["crawled_at"], datetime)
        self.commit_calls.append({**values, "candidates": tuple(candidates)})
        self.candidates.extend(candidates)
        return ["created"] * len(candidates)


class ListingHttpClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, tuple[str, ...] | None]] = []

    def reset_robots(self) -> None:
        return None

    def get(self, url: str, *, allowed_hosts: list[str] | None = None) -> str:
        self.calls.append(
            (url, tuple(allowed_hosts) if allowed_hosts is not None else None)
        )
        return self.content

    def submit_form(
        self,
        method: str,
        url: str,
        fields: dict[str, str],
        *,
        allowed_hosts: list[str] | None = None,
    ) -> str:
        assert method == "post"
        assert fields == {}
        self.calls.append(
            (url, tuple(allowed_hosts) if allowed_hosts is not None else None)
        )
        return self.content


def test_crawler_uses_html_adapter_host_scope_limit_and_disabled_detail(
    tmp_path: Path,
) -> None:
    source = information_college_config().model_dump(mode="json", exclude_none=True)
    source["max_items"] = 2
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump({"sources": [source]}, allow_unicode=True), encoding="utf-8"
    )
    repository = RecordingRepository()
    client = ListingHttpClient(FIXTURE_PATH.read_text(encoding="utf-8"))
    service = CrawlerService(
        config_path, repository, client, workspace_root=WORKSPACE_ROOT
    )  # type: ignore[arg-type]

    result = service.run_with_urls()
    summary = result.summary

    assert summary.created == 2
    assert [item.title for item in repository.candidates] == [
        "資訊學院測試公告一",
        "資訊學院測試公告二",
    ]
    assert client.calls == [
        (
            "https://ccs.nptu.edu.tw/p/403-1025-1019-1.php?Lang=zh-tw",
            ("ccs.nptu.edu.tw",),
        )
    ]
    assert result.canonical_urls == {
        "information-college-html": (
            "https://ccs.nptu.edu.tw/p/406-1025-197412,r1019.php?Lang=zh-tw",
            "https://ccs.nptu.edu.tw/p/406-1025-197411,r1019.php?Lang=zh-tw",
        )
    }


def test_crawler_refreshes_only_scholarship_tab_and_keeps_host_allowlist(
    tmp_path: Path,
) -> None:
    payload = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    source = next(
        item
        for item in payload["sources"]
        if item["name"] == "student-scholarship-external-html"
    )
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump({"sources": [source]}, allow_unicode=True), encoding="utf-8"
    )
    repository = RecordingRepository()
    fixture = BeautifulSoup(
        SCHOLARSHIP_FIXTURE_PATH.read_text(encoding="utf-8"),
        "html.parser",
    )
    external_tab = fixture.select_one("#cmb_1373_0")
    assert external_tab is not None
    client = ListingHttpClient(external_tab.decode_contents())
    service = CrawlerService(
        config_path, repository, client, workspace_root=WORKSPACE_ROOT
    )  # type: ignore[arg-type]

    result = service.run_with_urls()

    assert result.summary.created == 2
    assert [item.title for item in repository.candidates] == [
        "【獎助學金】外部基金會獎助學金公告",
        "【獎學金】校外公益獎學金申請公告",
    ]
    assert client.calls == [
        (
            "https://staf-life.nptu.edu.tw/app/index.php?Action=mobileloadmod&Type=mobile_rcg_mstr&Nbr=3893",
            ("staf-life.nptu.edu.tw",),
        )
    ]
    assert all(
        url.startswith("https://staf-life.nptu.edu.tw/")
        for url in result.canonical_urls["student-scholarship-external-html"]
    )


def test_crawler_commits_one_source_with_repository_bulk_transaction(
    tmp_path: Path,
) -> None:
    source = information_college_config().model_dump(mode="json", exclude_none=True)
    source["max_items"] = 2
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump({"sources": [source]}, allow_unicode=True),
        encoding="utf-8",
    )
    repository = BulkOnlyRepository()
    service = CrawlerService(
        config_path,
        repository,
        ListingHttpClient(FIXTURE_PATH.read_text(encoding="utf-8")),
        workspace_root=WORKSPACE_ROOT,
    )  # type: ignore[arg-type]

    result = service.run_with_urls()

    assert result.summary.created == 2
    assert repository.bulk_calls == 1
    assert [item.title for item in repository.candidates] == [
        "資訊學院測試公告一",
        "資訊學院測試公告二",
    ]


def test_crawler_commits_announcements_and_source_snapshot_through_one_repository_call(
    tmp_path: Path,
) -> None:
    source = information_college_config().model_dump(mode="json", exclude_none=True)
    source["max_items"] = 2
    config_path = tmp_path / "sources.yaml"
    config_path.write_text(
        yaml.safe_dump({"sources": [source]}, allow_unicode=True),
        encoding="utf-8",
    )
    repository = AtomicSnapshotRepository()
    service = CrawlerService(
        config_path,
        repository,
        ListingHttpClient(FIXTURE_PATH.read_text(encoding="utf-8")),
        workspace_root=WORKSPACE_ROOT,
    )  # type: ignore[arg-type]

    result = service.run_with_urls()

    assert result.summary.created == 2
    assert result.summary.failed == 0
    assert result.persisted_source_snapshots == frozenset({"information-college-html"})
    assert len(repository.commit_calls) == 1
    assert repository.commit_calls[0]["source_name"] == "information-college-html"
    assert (
        tuple(item.canonical_url for item in repository.candidates)
        == result.canonical_urls["information-college-html"]
    )
