from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nptu_assistant.core.settings import Settings
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.db.models import Document
from nptu_assistant.db.repositories import SqlAnnouncementRepository, SqlDocumentRepository
from nptu_assistant.db.session import create_session_factory
from nptu_assistant.ingestion.cleaning import extract_clean_html
from nptu_assistant.ingestion.parsers import parse_document
from nptu_assistant.ingestion.service import DocumentIngestionService
from nptu_assistant.main import create_app
from nptu_assistant.providers.fake import FakeEmbeddingProvider


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database with pgvector and pg_trgm",
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class NoNetworkHttpClient:
    def get(self, url: str) -> str:
        raise AssertionError(f"fixture attempted a network request: {url}")


def test_fixture_ingestion_crawl_and_grounded_chat() -> None:
    database_url = os.environ["DATABASE_URL"]
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        openai_api_key=None,
        llm_provider="fake",
        embedding_provider="fake",
        admin_api_enabled=True,
        admin_api_key="integration-admin-key",
        cors_allowed_origins="http://localhost:3000",
        official_documents_path="data/fixtures/official-documents",
        crawler_config_path="data/sources/announcements.yaml",
        crawler_request_interval_seconds=0,
    )
    headers = {"X-Admin-Key": "integration-admin-key"}

    with TestClient(create_app(settings=settings)) as client:
        ingestion = client.post("/v1/admin/ingest/documents", headers=headers)
        crawl = client.post(
            "/v1/admin/crawl/announcements",
            headers=headers,
            json={"source_names": ["local-fixture"]},
        )

        assert ingestion.status_code == 200
        assert ingestion.json()["failed"] == 0
        assert ingestion.json()["created"] + ingestion.json()["skipped"] >= 1
        assert crawl.status_code == 200
        assert crawl.json()["failed"] == 0
        assert crawl.json()["created"] + crawl.json()["unchanged"] >= 1

        document_question = parse_document(
            WORKSPACE_ROOT / "data/fixtures/official-documents/student-leave.md"
        )
        document_chat = client.post("/v1/chat", json={"question": document_question})

        assert document_chat.status_code == 200
        assert document_chat.json()["answer_type"] == "official_document"
        assert document_chat.json()["sources"]

        detail = extract_clean_html(
            (WORKSPACE_ROOT / "data/fixtures/announcements/detail.html").read_text(
                encoding="utf-8"
            )
        )
        announcement_chat = client.post("/v1/chat", json={"question": f"公告\n{detail}"})
        announcements = client.get("/v1/announcements?page=1&page_size=20")

        assert announcement_chat.status_code == 200
        assert announcement_chat.json()["answer_type"] == "announcement"
        assert announcement_chat.json()["sources"]
        assert announcements.status_code == 200
        assert announcements.json()["total"] >= 1
        published_dates = [item["published_at"] for item in announcements.json()["items"]]
        assert published_dates == sorted(published_dates, reverse=True)


def test_document_content_change_creates_a_version_chain(tmp_path: Path) -> None:
    unique = uuid.uuid4().hex
    canonical_url = f"https://www.nptu.edu.tw/integration-document-{unique}"
    directory = tmp_path / "documents"
    directory.mkdir()
    document = directory / "rule.md"
    metadata = directory / "rule.yaml"
    document.write_text("# 測試規定\n\n第一版內容。", encoding="utf-8")
    metadata.write_text(
        f"""title: 測試規定
source_url: {canonical_url}
unit: 測試單位
published_at: 2026-01-01
document_type: regulation
version: "1.0"
""",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    service = DocumentIngestionService(
        directory,
        SqlDocumentRepository(factory),
        FakeEmbeddingProvider(1536),
    )

    first = service.run()
    document.write_text("# 測試規定\n\n第二版內容。", encoding="utf-8")
    second = service.run()

    assert first.created == 1
    assert second.created == 1
    with factory() as session:
        versions = session.scalars(
            select(Document).where(Document.canonical_url == canonical_url)
        ).all()
    assert len(versions) == 2
    current = next(item for item in versions if item.is_current)
    previous = next(item for item in versions if not item.is_current)
    assert current.supersedes_document_id == previous.id


def test_announcement_content_change_reports_updated_then_unchanged(tmp_path: Path) -> None:
    unique = uuid.uuid4().hex
    canonical_url = f"https://www.nptu.edu.tw/integration-announcement-{unique}"
    fixture_dir = tmp_path / "announcements"
    fixture_dir.mkdir()
    fixture_dir.joinpath("overview.xml").write_text(
        f"""<?xml version="1.0"?><rss><channel><item>
        <title>整合測試公告</title><link>{canonical_url}</link>
        <description><![CDATA[<p>摘要內容</p>]]></description>
        <pubDate>2026-07-10</pubDate><author>測試單位</author>
        </item></channel></rss>""",
        encoding="utf-8",
    )
    detail = fixture_dir / "detail.html"
    detail.write_text("<main><h1>整合測試公告</h1><p>第一版內容</p></main>", encoding="utf-8")
    config = tmp_path / "sources.yaml"
    config.write_text(
        f"""sources:
  - name: integration-fixture-{unique}
    adapter: fixture
    url: announcements/overview.xml
    unit: 測試單位
    enabled: false
    crawl_interval_minutes: 60
""",
        encoding="utf-8",
    )
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    service = CrawlerService(
        config,
        SqlAnnouncementRepository(factory),
        NoNetworkHttpClient(),
        workspace_root=tmp_path,
    )

    first = service.run([f"integration-fixture-{unique}"])
    detail.write_text("<main><h1>整合測試公告</h1><p>第二版內容</p></main>", encoding="utf-8")
    second = service.run([f"integration-fixture-{unique}"])
    third = service.run([f"integration-fixture-{unique}"])

    assert first.created == 1
    assert second.updated == 1
    assert third.unchanged == 1
