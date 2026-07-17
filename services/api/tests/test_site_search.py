from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePageAdapter
from nptu_assistant.crawlers.config import SiteSearchConfig, load_keyword_search_config
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SitePageIngestionService,
)
from nptu_assistant.providers.fake import FakeEmbeddingProvider


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def site_config(**overrides: object) -> SiteSearchConfig:
    values: dict[str, object] = {
        "enabled": True,
        "name": "nptu-domain-search",
        "seed_urls": ["https://www.nptu.edu.tw/"],
        "allowed_hosts": ["nptu.edu.tw"],
        "max_pages": 10,
        "max_items": 5,
        "unit": "國立屏東大學",
        "category": "NPTU 網域搜尋",
    }
    values.update(overrides)
    return SiteSearchConfig.model_validate(values)


def test_project_site_search_is_enabled_and_root_scoped() -> None:
    config = load_keyword_search_config(WORKSPACE_ROOT / "data/sources/announcements.yaml")

    assert config.site_search is not None
    assert config.site_search.enabled is True
    assert config.site_search.seed_urls == ["https://www.nptu.edu.tw/"]
    assert config.site_search.allowed_hosts == ["nptu.edu.tw"]


def test_site_search_config_rejects_non_nptu_seed() -> None:
    with pytest.raises(ValueError, match="NPTU"):
        site_config(seed_urls=["https://example.com/"])


def test_site_page_adapter_extracts_date_and_only_crawlable_nptu_links() -> None:
    html = """
    <html><head>
      <title>校外獎學金公告</title>
      <meta property="article:published_time" content="2026-07-10T09:00:00">
    </head><body><main>
      <h1>校外獎學金公告</h1><p>提供獎學金申請資訊。</p>
      <a href="/p/next.php#content">下一頁</a>
      <a href="https://ccs.nptu.edu.tw/p/college.php">校內頁面</a>
      <a href="https://example.com/phishing">外部頁面</a>
      <a href="/files/rules.pdf">PDF</a>
    </main></body></html>
    """

    page = NptuSitePageAdapter().parse_page(
        html,
        "https://www.nptu.edu.tw/",
        allowed_hosts=["nptu.edu.tw"],
    )

    assert page.title == "校外獎學金公告"
    assert page.published_at == date(2026, 7, 10)
    assert page.links == (
        "https://www.nptu.edu.tw/p/next.php",
        "https://ccs.nptu.edu.tw/p/college.php",
    )
    assert "提供獎學金申請資訊" in page.body


class MappingHttpClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def get(self, url: str, *, allowed_hosts: list[str] | None = None) -> str:
        self.calls.append((url, tuple(allowed_hosts or ())))
        return self.pages[url]


def test_site_search_follows_only_allowlisted_links_and_matches_pages() -> None:
    pages = {
        "https://www.nptu.edu.tw/": """
        <main><h1>校首頁</h1><a href="/announcement.php">公告</a>
        <a href="https://example.com/out">外部</a></main>
        """,
        "https://www.nptu.edu.tw/announcement.php": """
        <main><h1>獎學金公告</h1><time datetime="2026-07-10">2026-07-10</time>
        <p>人工智慧獎學金申請資訊。</p></main>
        """,
    }
    http = MappingHttpClient(pages)

    result = NptuSiteSearchService(site_config(), http).search("人工智慧 獎學金")

    assert [page.canonical_url for page in result.pages] == [
        "https://www.nptu.edu.tw/announcement.php",
    ]
    assert result.visited_count == 2
    assert result.failed_count == 0
    assert all(hosts == ("nptu.edu.tw",) for _, hosts in http.calls)


class MemoryDocumentRepository:
    def __init__(self) -> None:
        self.hashes: set[tuple[str, str]] = set()
        self.saved: list[tuple[object, str]] = []

    def has_hash(self, canonical_url: str, digest: str) -> bool:
        return (canonical_url, digest) in self.hashes

    def save(self, metadata, raw_text, chunks, embeddings) -> None:
        assert len(chunks) == len(embeddings)
        self.hashes.add((str(metadata.source_url), metadata.version))
        self.saved.append((metadata, raw_text))


def test_site_page_ingestion_indexes_pages_without_pretending_they_are_announcements() -> None:
    html = "<main><h1>校務資訊</h1><p>人工智慧課程與申請說明。</p></main>"
    http = MappingHttpClient({"https://www.nptu.edu.tw/": html})
    config = site_config()
    search = NptuSiteSearchService(config, http)
    repository = MemoryDocumentRepository()

    result = SitePageIngestionService(
        search,
        repository,
        FakeEmbeddingProvider(1536),
        config,
    ).ingest("人工智慧", max_items=5)

    assert result.summary.created == 1
    assert result.summary.failed == 0
    assert len(repository.saved) == 1
    metadata, raw_text = repository.saved[0]
    assert metadata.document_type == "official_web_page"
    assert metadata.published_at is None
    assert metadata.effective_from == date.today()
    assert "人工智慧課程" in raw_text
