from __future__ import annotations

import os
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DataError

from nptu_assistant.core.settings import Settings
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.config import (
    SiteSearchConfig,
    load_keyword_search_config,
    load_source_configs,
)
from nptu_assistant.crawlers.official_units import (
    DocumentSearchScope,
    load_official_unit_directory,
)
from nptu_assistant.crawlers.resolution import UnitSourceResolver
from nptu_assistant.crawlers.service import CrawlerService
from nptu_assistant.crawlers.site_models import SearchDeadline, SearchPlan
from nptu_assistant.crawlers.site_search import (
    NptuSiteSearchService,
    SitePageIngestionService,
)
from nptu_assistant.api.schemas import AnswerType
from nptu_assistant.db.models import (
    Announcement,
    Conversation,
    ConversationEvent,
    Document,
    Source,
)
from nptu_assistant.db.repositories import (
    SqlAnnouncementRepository,
    SqlDocumentRepository,
)
from nptu_assistant.db.session import create_session_factory
from nptu_assistant.ingestion.chunking import TextChunk
from nptu_assistant.ingestion.cleaning import extract_clean_html
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.parsers import parse_document
from nptu_assistant.ingestion.service import DocumentIngestionService
from nptu_assistant.main import create_app
from nptu_assistant.providers.fake import FakeEmbeddingProvider
from nptu_assistant.rag.conversation import SqlConversationStore
from nptu_assistant.rag.models import Evidence
from nptu_assistant.rag.retrieval import SqlRetriever
from nptu_assistant.rag.tools import AnnouncementSort, ToolExecutor


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="requires a migrated PostgreSQL database with pgvector and pg_trgm",
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]


class NoNetworkHttpClient:
    def get(self, url: str) -> str:
        raise AssertionError(f"fixture attempted a network request: {url}")


class MappingNptuHttpClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages

    def get(
        self,
        url: str,
        *,
        allowed_hosts: object | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        del allowed_hosts, timeout_seconds, deadline
        return self.pages[url]


class ScopePoolEmbeddingProvider:
    def embed(
        self,
        texts: list[str],
        *,
        timeout_seconds: float | None = None,
    ) -> list[list[float]]:
        del timeout_seconds
        return [[1.0, *([0.0] * 1535)] for _text in texts]


def test_fixture_ingestion_crawl_and_grounded_chat(tmp_path: Path) -> None:
    database_url = os.environ["DATABASE_URL"]
    crawler_payload = yaml.safe_load(
        (WORKSPACE_ROOT / "data/sources/announcements.yaml").read_text(encoding="utf-8")
    )
    for source in crawler_payload["sources"]:
        source["enabled"] = False
    crawler_config = tmp_path / "announcements.yaml"
    crawler_config.write_text(
        yaml.safe_dump(crawler_payload, allow_unicode=True),
        encoding="utf-8",
    )
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
        crawler_config_path=str(crawler_config),
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
        announcement_chat = client.post(
            "/v1/chat", json={"question": f"公告\n{detail}"}
        )
        announcements = client.get("/v1/announcements?page=1&page_size=20")

        assert announcement_chat.status_code == 200
        assert all(
            source["url"] != "https://academic.nptu.edu.tw/p/406-1.php"
            for source in announcement_chat.json()["sources"]
        )
        assert announcements.status_code == 200
        assert announcements.json()["total"] >= 1
        published_dates = [
            item["published_at"] for item in announcements.json()["items"]
        ]
        assert published_dates == sorted(published_dates, reverse=True)


def test_structured_announcement_retrieval_filters_sorts_and_gets_by_id() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    unique = uuid.uuid4().hex
    unit = f"整合測試單位-{unique}"
    query = f"獎學金-{unique}"
    now = datetime.now(timezone.utc)

    with factory.begin() as session:
        public_source = Source(
            name=f"integration-live-{unique}",
            base_url="https://www.nptu.edu.tw",
            unit=unit,
            source_type="announcement",
            crawl_enabled=False,
            crawl_interval_minutes=60,
        )
        fixture_source = Source(
            name=f"integration-fixture-{unique}",
            base_url="https://www.nptu.edu.tw",
            unit=unit,
            source_type="announcement",
            crawl_enabled=False,
            crawl_interval_minutes=60,
        )
        session.add_all([public_source, fixture_source])
        session.flush()
        public_items = []
        for index in range(6):
            item = Announcement(
                source_id=public_source.id,
                title=f"{query} 第 {index + 1} 則",
                unit=unit,
                category=None,
                published_at=date(2026, 7, index + 1),
                deadline_at=None,
                canonical_url=f"https://www.nptu.edu.tw/{unique}/{index}",
                body=f"{query} 內容 {index + 1}",
                warning=None,
                content_hash=f"{index:064x}",
                last_crawled_at=now + timedelta(minutes=index),
            )
            public_items.append(item)
            session.add(item)
        fixture = Announcement(
            source_id=fixture_source.id,
            title=f"{query} fixture",
            unit=unit,
            category=None,
            published_at=date(2026, 7, 31),
            deadline_at=None,
            canonical_url=f"https://www.nptu.edu.tw/{unique}/fixture",
            body=f"{query} fixture content",
            warning=None,
            content_hash="f" * 64,
            last_crawled_at=now,
        )
        session.add(fixture)
        session.flush()
        public_ids = [str(item.id) for item in public_items]
        fixture_id = str(fixture.id)
        fixture_url = fixture.canonical_url

    retriever = SqlRetriever(factory, FakeEmbeddingProvider(1536))
    newest = retriever.search_announcements(
        query=None,
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=unit,
    )
    relevance = retriever.search_announcements(
        query=query,
        limit=3,
        sort=AnnouncementSort.RELEVANCE,
        unit=unit,
    )
    oldest = retriever.search_announcements(
        query=None,
        limit=2,
        sort=AnnouncementSort.OLDEST,
        unit=unit,
    )
    ranged = retriever.search_announcements(
        query=None,
        limit=20,
        sort=AnnouncementSort.NEWEST,
        unit=unit,
        date_from=date(2026, 7, 2),
        date_to=date(2026, 7, 3),
    )

    assert [item.id for item in newest] == list(reversed(public_ids))[:5]
    assert len(relevance) == 3
    assert all(query in item.title for item in relevance)
    assert [item.id for item in oldest] == public_ids[:2]
    assert [item.published_at for item in ranged] == [
        date(2026, 7, 3),
        date(2026, 7, 2),
    ]
    assert all(item.url != fixture_url for item in newest + relevance + oldest + ranged)
    assert retriever.get_announcement(public_ids[0]).id == public_ids[0]
    assert retriever.get_announcement(fixture_id) is None
    assert retriever.get_announcement(str(uuid.uuid4())) is None


def test_postgres_conversation_store_redacts_expires_and_deletes() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    store = SqlConversationStore(factory)
    context = store.load_or_create(None)
    source = Evidence(
        id=str(uuid.uuid4()),
        kind=AnswerType.ANNOUNCEMENT,
        title="測試公告",
        url="https://www.nptu.edu.tw/conversation-test",
        unit="教務處",
        published_at=date(2026, 7, 12),
        content="內容",
        score=0.8,
    )

    store.save_turn(
        conversation_id=context.conversation_id,
        user_message="我的學號 123456789，第三則是什麼？",
        assistant_message="第三則是測試公告。",
        warning=None,
        tool_events=[{"tool_name": "search_announcements", "evidence": [source]}],
        sources=[source],
    )
    loaded = store.load_or_create(context.conversation_id)

    assert any(item.get("role") == "assistant" for item in loaded.input_items)
    assert loaded.evidence[0].id == source.id
    with factory.begin() as session:
        user_event = session.scalar(
            select(ConversationEvent).where(
                ConversationEvent.conversation_id == uuid.UUID(context.conversation_id),
                ConversationEvent.event_type == "user",
            )
        )
        assert user_event.content == "[已隱去可能的個人或敏感資料]"
        conversation = session.get(Conversation, uuid.UUID(context.conversation_id))
        conversation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    replacement = store.load_or_create(context.conversation_id)

    assert replacement.conversation_id != context.conversation_id
    with factory() as session:
        assert session.get(Conversation, uuid.UUID(context.conversation_id)) is None
    assert store.delete(replacement.conversation_id) is True
    assert store.delete(replacement.conversation_id) is False


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


def test_postgres_multi_query_document_retrieval_uses_vector_trigram_and_rrf() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    repository = SqlDocumentRepository(factory)
    embedding = FakeEmbeddingProvider(1536)
    retriever = SqlRetriever(factory, embedding)
    unique = uuid.uuid4().hex
    admission_url = f"https://www.nptu.edu.tw/{unique}/admission"
    dormitory_url = f"https://www.nptu.edu.tw/{unique}/dormitory-electricity"
    admission_chunks = [
        TextChunk(
            sequence=0,
            content="大學申請入學新生專區，錄取生應依規定完成網路報到。",
            token_count=24,
        ),
        TextChunk(
            sequence=1,
            content="申請入學錄取新生須在期限內完成報到程序。",
            token_count=22,
        ),
    ]
    dormitory_chunks = [
        TextChunk(
            sequence=0,
            content="學生宿舍用電依度數與公告收費標準計算。",
            token_count=21,
        )
    ]
    repository.save(
        DocumentMetadata(
            title="大學申請入學新生專區",
            source_url=admission_url,
            unit="教務處",
            effective_from=date.today(),
            document_type="official_web_page",
            version=unique,
        ),
        "\n".join(chunk.content for chunk in admission_chunks),
        admission_chunks,
        embedding.embed([chunk.content for chunk in admission_chunks]),
    )
    repository.save(
        DocumentMetadata(
            title="住宿服務中心學生宿舍用電計費辦法",
            source_url=dormitory_url,
            unit="學務處",
            effective_from=date.today(),
            document_type="official_web_page",
            version=unique,
        ),
        dormitory_chunks[0].content,
        dormitory_chunks,
        embedding.embed([dormitory_chunks[0].content]),
    )

    admission = retriever.search_documents_with_plan(
        plan=SearchPlan(
            query="個人申請新生資訊",
            search_queries=["大學申請入學", "申請入學新生報到"],
            concepts=["個人申請", "申請入學", "新生", "報到"],
            limit=6,
        ),
        limit=6,
        deadline=SearchDeadline.after(10),
    )
    dormitory = retriever.search_documents_with_plan(
        plan=SearchPlan(
            query="學生宿舍冷氣費怎麼計算",
            search_queries=["宿舍電費計價", "學生宿舍用電收費標準"],
            concepts=["學生宿舍", "冷氣", "電費", "收費"],
            limit=6,
        ),
        limit=6,
        deadline=SearchDeadline.after(10),
    )

    assert admission[0].url == admission_url
    assert dormitory[0].url == dormitory_url
    assert sum(item.url == admission_url for item in admission) == 1
    assert len({item.url for item in admission}) == len(admission)
    assert all(0.0 <= item.score <= 1.0 for item in admission + dormitory)


def test_postgres_scoped_candidate_pools_recover_target_beyond_global_top_20() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    repository = SqlDocumentRepository(factory)
    embedding = ScopePoolEmbeddingProvider()
    retriever = SqlRetriever(factory, embedding)
    unique = uuid.uuid4().hex
    query = f"精確命中主題-{unique}"
    target_unit = f"目標學系-{unique}"
    target_host = "csie.nptu.edu.tw"

    for index in range(25):
        content = f"{query} {query} 全域干擾文件 {index}"
        repository.save(
            DocumentMetadata(
                title=f"{query} 干擾文件 {index}",
                source_url=f"https://www.nptu.edu.tw/{unique}/distractor-{index}",
                unit=f"其他單位-{index}",
                effective_from=date.today(),
                document_type="official_web_page",
                version=unique,
            ),
            content,
            [TextChunk(sequence=0, content=content, token_count=12)],
            embedding.embed([content]),
        )

    target_url = f"https://{target_host}/{unique}/target"
    target_content = f"{query} 目標學系正式文件"
    repository.save(
        DocumentMetadata(
            title="目標學系正式文件",
            source_url=target_url,
            unit=target_unit,
            effective_from=date.today(),
            document_type="official_web_page",
            version=unique,
        ),
        target_content,
        [TextChunk(sequence=0, content=target_content, token_count=10)],
        [[0.8, 0.6, *([0.0] * 1534)]],
    )

    evidence = retriever.search_documents_with_plan(
        plan=SearchPlan.from_query(query, limit=6),
        limit=6,
        deadline=SearchDeadline.after(10),
        scope=DocumentSearchScope(
            canonical_unit=target_unit,
            homepage_url=f"https://{target_host}/",
            preferred_hosts=(target_host,),
            allowed_hosts=(target_host,),
            seed_urls=(f"https://{target_host}/",),
        ),
    )

    assert evidence[0].url == target_url
    assert evidence[0].unit == target_unit
    assert all(0.0 <= item.score <= 1.0 for item in evidence)


def test_scoped_fixture_persists_then_returns_database_id_and_detail() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    announcement_repository = SqlAnnouncementRepository(factory)
    document_repository = SqlDocumentRepository(factory)
    retriever = SqlRetriever(factory, FakeEmbeddingProvider(1536))
    unique = uuid.uuid4().hex
    host = "csie.nptu.edu.tw"
    homepage = f"https://{host}/"
    listing_url = f"https://{host}/p/403-{unique}.php"
    ai_url = f"https://{host}/p/406-{unique}-ai.php"
    general_url = f"https://{host}/p/406-{unique}-general.php"
    old_url = f"https://{host}/p/406-{unique}-cached.php"
    pages = {
        homepage: (
            f'<main><h1>資訊工程學系</h1><a href="{listing_url}">最新公告</a></main>'
        ),
        listing_url: f"""
            <main><h1>資訊工程學系最新公告</h1><div class="module">
              <div class="row listBS"><span class="mtitle"><a href="{ai_url}">人工智慧專題講座</a></span><i class="mdate">2026-07-25</i></div>
              <div class="row listBS"><span class="mtitle"><a href="{general_url}">一般學系活動</a></span><i class="mdate">2026-07-20</i></div>
            </div></main>
        """,
        ai_url: """
            <html><head><meta property="article:published_time" content="2026-07-10"></head>
            <body><article><h1>人工智慧專題講座</h1>
            <p>人工智慧專題講座正式公告內容，包含完整活動資訊與參加方式。</p></article></body></html>
        """,
        general_url: """
            <html><head><meta property="article:published_time" content="2026-07-20"></head>
            <body><article><h1>一般學系活動</h1>
            <p>一般學系活動正式公告內容，包含完整活動資訊與參加方式。</p></article></body></html>
        """,
    }
    site_config = SiteSearchConfig.model_validate(
        {
            "enabled": True,
            "name": "integration-scoped-site-search",
            "seed_urls": [homepage],
            "allowed_hosts": [host],
            "max_pages": 2,
            "max_pages_per_host": 2,
            "max_depth": 1,
            "max_items": 5,
            "relevance_threshold": 0.0,
            "early_stop_min_results": 2,
            "unit": "國立屏東大學",
            "category": "NPTU 網域搜尋",
        }
    )
    site_search = NptuSiteSearchService(
        site_config,
        MappingNptuHttpClient(pages),
    )
    site_ingestor = SitePageIngestionService(
        site_search,
        document_repository,
        FakeEmbeddingProvider(1536),
        site_config,
        announcement_repository,
    )
    unit_directory = load_official_unit_directory(
        WORKSPACE_ROOT / "data/sources/official_units.yaml"
    )
    crawler_config = WORKSPACE_ROOT / "data/sources/announcements.yaml"
    keyword_config = load_keyword_search_config(crawler_config)
    resolver = UnitSourceResolver(
        load_source_configs(crawler_config),
        keyword_config.aliases,
        keyword_config.source_routes,
        unit_directory,
    )
    executor = ToolExecutor(
        retriever,
        unit_resolver=resolver,
        site_page_ingestor=site_ingestor,
    )
    announcement_repository.merge_source_announcements(
        [
            AnnouncementCandidate(
                title="既有快取公告",
                canonical_url=old_url,
                unit="資訊工程學系",
                category="單位公告",
                published_at=date(2026, 7, 1),
                deadline_at=None,
                body="既有快取內容",
            )
        ],
        source_name="unit-scoped:資訊工程學系",
        source_url=homepage,
        source_unit="資訊工程學系",
        interval_minutes=60,
        crawled_at=datetime.now(timezone.utc),
    )

    latest = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": None,
                "limit": 2,
                "sort": "newest",
                "unit": "資工系",
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
    )
    relevant = executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": "資工系人工智慧公告",
                "limit": 2,
                "sort": "relevance",
                "unit": "資工系",
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
    )

    assert [item.url for item in latest.evidence] == [general_url, ai_url]
    assert relevant.evidence[0].url == ai_url
    assert latest.evidence[1].published_at == date(2026, 7, 10)
    assert all(
        item.id != str(uuid.uuid5(uuid.NAMESPACE_URL, item.url))
        for item in latest.evidence
    )
    detail = executor.execute(
        "get_announcement",
        json.dumps({"announcement_id": latest.evidence[0].id}),
    )
    assert detail.evidence[0].id == latest.evidence[0].id
    assert detail.evidence[0].url == general_url

    executor.execute(
        "search_announcements",
        json.dumps(
            {
                "query": None,
                "limit": 2,
                "sort": "newest",
                "unit": "資工系",
                "date_from": None,
                "date_to": None,
            },
            ensure_ascii=False,
        ),
    )
    with factory() as session:
        stored_count = len(
            session.scalars(
                select(Announcement).where(
                    Announcement.canonical_url.in_([ai_url, general_url])
                )
            ).all()
        )
    assert stored_count == 2
    assert set(
        announcement_repository.canonical_urls_for_source("unit-scoped:資訊工程學系")
        or ()
    ).issuperset({old_url, ai_url, general_url})


def test_announcement_content_change_reports_updated_then_unchanged(
    tmp_path: Path,
) -> None:
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
    detail.write_text(
        "<main><h1>整合測試公告</h1><p>第一版內容</p></main>", encoding="utf-8"
    )
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
    repository = SqlAnnouncementRepository(factory)
    service = CrawlerService(
        config,
        repository,
        NoNetworkHttpClient(),
        workspace_root=tmp_path,
    )

    first = service.run([f"integration-fixture-{unique}"])
    detail.write_text(
        "<main><h1>整合測試公告</h1><p>第二版內容</p></main>", encoding="utf-8"
    )
    second = service.run([f"integration-fixture-{unique}"])
    third = service.run([f"integration-fixture-{unique}"])

    assert first.created == 1
    assert second.updated == 1
    assert third.unchanged == 1
    assert repository.canonical_urls_for_source(f"integration-fixture-{unique}") == (
        canonical_url,
    )
    assert repository.latest_crawled_at(f"integration-fixture-{unique}") is not None


def test_announcement_source_snapshot_tri_state_and_metadata_only_update() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    repository = SqlAnnouncementRepository(factory)
    unique = uuid.uuid4().hex
    source_name = f"integration-source-snapshot-{unique}"
    source_url = "https://ccs.nptu.edu.tw/index.php"
    canonical_url = f"https://ccs.nptu.edu.tw/{unique}/announcement"
    crawled_at = datetime.now(timezone.utc)

    assert repository.latest_crawled_at(source_name) is None
    assert repository.canonical_urls_for_source(source_name) is None

    repository.record_source_refresh(
        source_name=source_name,
        source_url=source_url,
        unit="資訊學院",
        interval_minutes=60,
        canonical_urls=(),
        crawled_at=crawled_at,
    )

    assert repository.latest_crawled_at(source_name) == crawled_at
    assert repository.canonical_urls_for_source(source_name) == ()

    ordered_urls = (
        canonical_url,
        f"https://ccs.nptu.edu.tw/{unique}/second",
        canonical_url,
    )
    repository.record_source_refresh(
        source_name=source_name,
        source_url=source_url,
        unit="資訊學院",
        interval_minutes=60,
        canonical_urls=ordered_urls,
        crawled_at=crawled_at + timedelta(minutes=1),
    )
    assert repository.canonical_urls_for_source(source_name) == ordered_urls[:2]

    original = AnnouncementCandidate(
        title="快照整合測試公告",
        canonical_url=canonical_url,
        unit="舊單位",
        category="舊分類",
        published_at=date(2026, 7, 1),
        deadline_at=None,
        body="內容不變",
        warning=None,
    )
    updated_metadata = AnnouncementCandidate(
        title=original.title,
        canonical_url=canonical_url,
        unit="資訊學院",
        category="學術單位公告",
        published_at=date(2026, 7, 2),
        deadline_at=date(2026, 7, 31),
        body=original.body,
        warning="測試警告",
    )

    assert (
        repository.upsert(
            original,
            source_name=source_name,
            source_url=source_url,
            interval_minutes=60,
        )
        == "created"
    )
    assert (
        repository.upsert(
            updated_metadata,
            source_name=source_name,
            source_url=source_url,
            interval_minutes=60,
        )
        == "unchanged"
    )

    with factory() as session:
        stored = session.scalar(
            select(Announcement).where(Announcement.canonical_url == canonical_url)
        )
    assert stored is not None
    assert stored.unit == "資訊學院"
    assert stored.category == "學術單位公告"
    assert stored.published_at == date(2026, 7, 2)
    assert stored.deadline_at == date(2026, 7, 31)
    assert stored.warning == "測試警告"


def test_bulk_announcement_upsert_rolls_back_every_item_on_one_database_error() -> None:
    settings = Settings(_env_file=None, database_url=os.environ["DATABASE_URL"])
    factory = create_session_factory(settings)
    repository = SqlAnnouncementRepository(factory)
    unique = uuid.uuid4().hex
    good_url = f"https://ccs.nptu.edu.tw/{unique}/good"
    bad_url = f"https://ccs.nptu.edu.tw/{unique}/bad"
    candidates = [
        AnnouncementCandidate(
            title="可寫入公告",
            canonical_url=good_url,
            unit="資訊學院",
            category="學術單位公告",
            published_at=date(2026, 7, 13),
            deadline_at=None,
            body="正常內容",
        ),
        AnnouncementCandidate(
            title="過長" * 251,
            canonical_url=bad_url,
            unit="資訊學院",
            category="學術單位公告",
            published_at=date(2026, 7, 12),
            deadline_at=None,
            body="會觸發資料庫長度限制",
        ),
    ]

    source_name = f"integration-atomic-{unique}"
    crawled_at = datetime.now(timezone.utc)

    with pytest.raises(DataError):
        repository.commit_source_refresh(
            candidates,
            source_name=source_name,
            source_url="https://ccs.nptu.edu.tw/index.php",
            source_unit="資訊學院",
            interval_minutes=60,
            crawled_at=crawled_at,
        )

    with factory() as session:
        stored_urls = session.scalars(
            select(Announcement.canonical_url).where(
                Announcement.canonical_url.in_([good_url, bad_url])
            )
        ).all()
    assert stored_urls == []
    assert repository.latest_crawled_at(source_name) is None
    assert repository.canonical_urls_for_source(source_name) is None
