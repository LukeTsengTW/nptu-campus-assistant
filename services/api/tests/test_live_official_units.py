from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from nptu_assistant.crawlers.adapters.factory import build_adapter
from nptu_assistant.crawlers.adapters.nptu_site import (
    NptuSitePageAdapter,
    UnitAnnouncementPageRole,
)
from nptu_assistant.crawlers.config import (
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.http import CrawlHttpClient
from nptu_assistant.crawlers.official_units import load_official_unit_directory
from nptu_assistant.crawlers.site_models import SearchPlan
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SitePageIngestionService,
)
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.core.settings import Settings
from nptu_assistant.db.repositories import SqlAnnouncementRepository
from nptu_assistant.db.session import create_session_factory
from nptu_assistant.providers.fake import FakeEmbeddingProvider
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import AnnouncementSort


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_NPTU_LIVE_SMOKE") != "1",
    reason="requires explicit NPTU live smoke opt-in",
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
ANNOUNCEMENT_CONFIG = WORKSPACE_ROOT / "data/sources/announcements.yaml"
UNIT_CONFIG = WORKSPACE_ROOT / "data/sources/official_units.yaml"


class LiveMemoryAnnouncementRepository:
    def __init__(self) -> None:
        self.items: dict[str, object] = {}

    def merge_source_announcements(self, candidates, **kwargs: object) -> list[str]:
        del kwargs
        for candidate in candidates:
            self.items[candidate.canonical_url] = candidate
        return ["created" for _candidate in candidates]


@pytest.mark.parametrize(
    "canonical_name",
    ["資訊學院", "教育學院", "理學院", "國際學院", "大武山學院"],
)
def test_live_representative_unit_homepage_and_announcements(
    canonical_name: str,
) -> None:
    directory = load_official_unit_directory(UNIT_CONFIG)
    unit = directory.get(canonical_name)
    assert unit is not None
    assert unit.homepage_url is not None
    client = CrawlHttpClient(
        "NPTU-Campus-Assistant-Official-Unit-Smoke/0.1",
        interval_seconds=1,
    )
    try:
        html = client.get_html(unit.homepage_url, allowed_hosts=unit.allowed_hosts)
        page = NptuSitePageAdapter().parse_page(
            html,
            unit.homepage_url,
            allowed_hosts=unit.allowed_hosts,
        )
        assert page.body
        assert any(host in page.canonical_url for host in unit.allowed_hosts)

        records = []
        listing_urls: set[str] = set()
        if unit.announcement_strategy.value == "scoped_site_search":
            base = load_keyword_search_config(ANNOUNCEMENT_CONFIG).site_search
            assert base is not None
            config = base.model_copy(
                update={
                    "max_pages": 5,
                    "max_items": 3,
                    "max_candidate_urls": 20,
                    "early_stop_min_results": 3,
                }
            )
            service = NptuSiteSearchService(config, client)
            result = service.search(
                SearchPlan.from_query(f"{canonical_name} 最新公告", limit=3),
                max_items=3,
                use_discovery=False,
                scope=directory.scope_for(unit),
            )
            listing_urls = {
                item.canonical_url
                for item in result.pages
                if item.role is UnitAnnouncementPageRole.LISTING
            }
            repository = LiveMemoryAnnouncementRepository()
            ingestion = SitePageIngestionService(
                service,
                object(),  # type: ignore[arg-type]
                FakeEmbeddingProvider(1536),
                config,
                repository,  # type: ignore[arg-type]
            )
            scoped = ingestion.search_unit_announcements(
                SearchPlan.from_query(f"{canonical_name} 最新公告", limit=2),
                scope=directory.scope_for(unit),
                max_items=2,
                deadline=ingestion.new_deadline(),
                sort="newest",
            )
            assert scoped.canonical_urls
            records = [repository.items[url] for url in scoped.canonical_urls]
        else:
            source = next(
                item
                for item in load_source_configs(ANNOUNCEMENT_CONFIG)
                if item.name == unit.announcement_source_name
            )
            listing_urls.add(source.url)
            adapter = build_adapter(source)
            listing_html = client.get(
                source.url,
                allowed_hosts=source.allowed_hosts,
            )
            records = adapter.parse_listing(listing_html)[:2]
            for record in records:
                detail_html = client.get(
                    record.canonical_url,
                    allowed_hosts=source.allowed_hosts,
                )
                assert adapter.parse_detail(detail_html)

        assert 1 <= len(records) <= 2
        assert all(record.canonical_url not in listing_urls for record in records)
        assert all(record.canonical_url not in unit.seed_urls for record in records)
        assert all(record.title and record.published_at for record in records)
        assert all(
            urlsplit(record.canonical_url).hostname in unit.allowed_hosts
            for record in records
        )
        published_dates = [record.published_at for record in records]
        assert published_dates == sorted(published_dates, reverse=True)
    finally:
        client.close()


def test_live_configured_listing_persists_and_gets_database_announcement() -> None:
    source = next(
        item
        for item in load_source_configs(ANNOUNCEMENT_CONFIG)
        if item.name == "information-college-html"
    )
    client = CrawlHttpClient(
        "NPTU-Campus-Assistant-Official-Unit-Smoke/0.1",
        interval_seconds=1,
    )
    try:
        settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
        factory = create_session_factory(settings)
        repository = SqlAnnouncementRepository(factory)
        summary = CrawlerService(
            ANNOUNCEMENT_CONFIG,
            repository,
            client,
            workspace_root=WORKSPACE_ROOT,
        ).run([source.name])
        canonical_urls = repository.canonical_urls_for_source(source.name)
        retriever = SqlRetriever(factory, FakeEmbeddingProvider(1536))
        items = retriever.search_announcements(
            query=None,
            limit=2,
            sort=AnnouncementSort.NEWEST,
            unit=source.unit,
            canonical_urls=canonical_urls,
        )
    finally:
        client.close()

    assert summary.failed == 0
    assert summary.created + summary.updated + summary.unchanged >= 1
    assert canonical_urls
    assert items
    assert all(item.unit == "資訊學院" for item in items)
    assert all(urlsplit(item.url).hostname in source.allowed_hosts for item in items)
    assert all(item.title and item.published_at for item in items)
    assert [item.published_at for item in items] == sorted(
        [item.published_at for item in items], reverse=True
    )
    detail = retriever.get_announcement(items[0].id)
    assert detail is not None
    assert detail.id == items[0].id
    assert detail.url == items[0].url
