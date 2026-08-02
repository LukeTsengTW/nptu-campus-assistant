from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from nptu_assistant.crawlers.adapters.nptu_site import NptuSitePage
from nptu_assistant.crawlers.crawl_ingestion import (
    CrawlIngestionService,
    CrawlIngestionStatus,
)
from nptu_assistant.ingestion.cleaning import content_hash


@dataclass
class SavedDocument:
    canonical_url: str
    raw_text: str
    is_current: bool = True


class MemoryDocumentRepository:
    def __init__(self) -> None:
        self.documents: list[SavedDocument] = []
        self.fail_urls: set[str] = set()

    def has_hash(self, canonical_url: str, digest: str) -> bool:
        return any(
            document.canonical_url == canonical_url
            and content_hash(document.raw_text) == digest
            for document in self.documents
        )

    def save(self, metadata, raw_text, chunks, embeddings) -> None:
        assert len(chunks) == len(embeddings)
        if str(metadata.source_url) in self.fail_urls:
            raise RuntimeError("document transaction failed")
        for document in self.documents:
            if document.canonical_url == str(metadata.source_url):
                document.is_current = False
        self.documents.append(SavedDocument(str(metadata.source_url), raw_text))


class RecordingEmbeddingProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str], *, timeout_seconds=None) -> list[list[float]]:
        del timeout_seconds
        self.calls.append(texts)
        return [[0.0] * 1536 for _ in texts]


def page(body: str, url: str = "https://www.nptu.edu.tw/rules") -> NptuSitePage:
    return NptuSitePage(
        title="校務規章",
        canonical_url=url,
        body=body,
        published_at=date(2026, 8, 1),
        links=(),
    )


def test_crawl_ingestion_only_embeds_changed_content() -> None:
    repository = MemoryDocumentRepository()
    embeddings = RecordingEmbeddingProvider()
    service = CrawlIngestionService(repository, embeddings, default_unit="教務處")

    first = service.ingest_page(page("第一版內容"))
    unchanged = service.ingest_page(page("第一版內容"))
    changed = service.ingest_page(page("第二版內容"))

    assert first.status is CrawlIngestionStatus.CREATED
    assert unchanged.status is CrawlIngestionStatus.SKIPPED
    assert changed.status is CrawlIngestionStatus.CREATED
    assert len(embeddings.calls) == 2
    assert [document.is_current for document in repository.documents] == [
        False,
        True,
    ]


def test_crawl_ingestion_deduplicates_same_page_in_one_batch() -> None:
    repository = MemoryDocumentRepository()
    embeddings = RecordingEmbeddingProvider()
    service = CrawlIngestionService(repository, embeddings, default_unit="教務處")

    summary = service.ingest_pages([page("相同內容"), page("相同內容")])

    assert summary.created == 1
    assert summary.skipped == 1
    assert summary.failed == 0
    assert len(repository.documents) == 1
    assert len(embeddings.calls) == 1


def test_crawl_ingestion_failure_keeps_last_success_and_continues_batch() -> None:
    repository = MemoryDocumentRepository()
    embeddings = RecordingEmbeddingProvider()
    service = CrawlIngestionService(repository, embeddings, default_unit="教務處")
    service.ingest_page(page("最後成功版本"))

    failed_page = page("無法保存的新版本")
    other_page = page("另一頁內容", "https://www.nptu.edu.tw/other")
    repository.fail_urls.add(failed_page.canonical_url)
    summary = service.ingest_pages([failed_page, other_page])

    assert summary.created == 1
    assert summary.failed == 1
    assert len(repository.documents) == 2
    assert repository.documents[0].raw_text == "最後成功版本"
    assert repository.documents[0].is_current is True
    assert failed_page.canonical_url in summary.errors[0]
