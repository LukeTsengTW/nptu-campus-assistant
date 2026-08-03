from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread

from fastapi.testclient import TestClient
from nptu_assistant.api.crawl_admin import CrawlWorkerController
from nptu_assistant.api.schemas import (
    AnnouncementListResponse,
    CrawlScheduleRequest,
    CrawlSummary,
    IngestionSummary,
)
from nptu_assistant.core.settings import Settings
from nptu_assistant.main import create_app


class StubHealth:
    def check(self) -> dict[str, object]:
        return {"status": "degraded", "checks": {"database": "ok"}}


class StubAnnouncements:
    def list(self, **kwargs: object) -> AnnouncementListResponse:
        del kwargs
        return AnnouncementListResponse(items=[], page=1, page_size=20, total=0)


class StubOperation:
    def run(
        self, source_names: list[str] | None = None
    ) -> IngestionSummary | CrawlSummary:
        del source_names
        return IngestionSummary(created=0)


class RecordingAdmin:
    def __init__(self) -> None:
        self.requests: list[CrawlScheduleRequest] = []

    def status(self) -> dict[str, object]:
        return {"status": "idle", "enabled": True, "runs_total": 2}

    def schedule(self, request: CrawlScheduleRequest) -> dict[str, object]:
        self.requests.append(request)
        return {
            "status": "scheduled",
            "schedule_id": "schedule-1",
            "scheduled_at": "2026-08-02T00:00:00Z",
            "run_at": "2026-08-02T00:00:00Z",
            "source_names": request.source_names or [],
            "dry_run": request.dry_run,
        }


def make_client(admin: RecordingAdmin) -> TestClient:
    settings = Settings(
        _env_file=None,
        admin_api_enabled=True,
        admin_api_key="test-admin-key",
        openai_api_key=None,
    )
    return TestClient(
        create_app(
            settings=settings,
            health_service=StubHealth(),
            chat_service=object(),
            announcement_service=StubAnnouncements(),
            ingestion_service=StubOperation(),
            crawler_service=StubOperation(),
            crawl_admin_service=admin,
        )
    )


def test_status_and_schedule_are_admin_authorized() -> None:
    admin = RecordingAdmin()
    client = make_client(admin)

    denied_status = client.get("/v1/admin/crawl/status")
    denied_schedule = client.post("/v1/admin/crawl/schedule")
    status = client.get(
        "/v1/admin/crawl/status",
        headers={"X-Admin-Key": "test-admin-key"},
    )
    scheduled = client.post(
        "/v1/admin/crawl/schedule",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"source_names": ["nptu-overview"], "dry_run": True},
    )

    assert denied_status.status_code == 401
    assert denied_schedule.status_code == 401
    assert status.status_code == 200
    assert status.json()["runs_total"] == 2
    assert scheduled.status_code == 202
    assert scheduled.json()["status"] == "scheduled"
    assert admin.requests[0].source_names == ["nptu-overview"]
    assert admin.requests[0].dry_run is True


def test_status_exposes_durable_pending_and_attempt_counts() -> None:
    class DurableAdmin(RecordingAdmin):
        def status(self) -> dict[str, object]:
            return {
                "status": "idle",
                "enabled": True,
                "pending": 4,
                "due": 2,
                "leased": 1,
                "active_workers": 1,
                "recent_attempts": {
                    "success_changed": 3,
                    "failed_transient": 1,
                },
            }

    response = make_client(DurableAdmin()).get(
        "/v1/admin/site-map/crawl/status",
        headers={"X-Admin-Key": "test-admin-key"},
    )

    assert response.status_code == 200
    assert response.json()["pending"] == 4
    assert response.json()["active_workers"] == 1
    assert response.json()["recent_attempts"] == {
        "success_changed": 3,
        "failed_transient": 1,
    }


def test_admin_aliases_and_schedule_validation_remain_compatible() -> None:
    client = make_client(RecordingAdmin())
    headers = {"X-Admin-Key": "test-admin-key"}

    old_status = client.get("/v1/admin/crawl/status", headers=headers)
    site_map_status = client.get("/v1/admin/site-map/crawl/status", headers=headers)
    invalid = client.post(
        "/v1/admin/site-map/crawl/schedule",
        headers=headers,
        json={"urls": ["https://example.com/not-allowed"]},
    )

    assert old_status.status_code == 200
    assert site_map_status.status_code == 200
    assert invalid.status_code == 422


def test_schedule_endpoint_only_submits_to_control_plane() -> None:
    class NoCrawler:
        def run(self, source_names: list[str] | None = None) -> CrawlSummary:
            raise AssertionError(f"不應在 request 執行 crawler：{source_names}")

    admin = RecordingAdmin()
    settings = Settings(
        _env_file=None,
        admin_api_enabled=True,
        admin_api_key="test-admin-key",
        openai_api_key=None,
    )
    client = TestClient(
        create_app(
            settings=settings,
            health_service=StubHealth(),
            chat_service=object(),
            announcement_service=StubAnnouncements(),
            ingestion_service=StubOperation(),
            crawler_service=NoCrawler(),
            crawl_admin_service=admin,
        )
    )

    response = client.post(
        "/v1/admin/crawl/schedule",
        headers={"X-Admin-Key": "test-admin-key"},
        json={"delay_seconds": 30},
    )

    assert response.status_code == 202
    assert len(admin.requests) == 1


def test_worker_dry_run_is_observable_without_refreshing() -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.calls = 0

        def refresh_due_sources(self) -> list[object]:
            self.calls += 1
            return []

    coordinator = Coordinator()
    worker = CrawlWorkerController(
        coordinator,
        source_names=lambda: ["nptu-overview"],
        now=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    report = worker.run_once(dry_run=True)
    status = worker.status()

    assert report["status"] == "dry_run"
    assert report["dry_run"] is True
    assert report["source_names"] == ["nptu-overview"]
    assert coordinator.calls == 0
    assert status["runs_total"] == 1
    assert status["last_duration_ms"] is not None


def test_worker_rejects_concurrent_run_once_reentry() -> None:
    started = Event()
    release = Event()

    class Coordinator:
        def refresh_due_sources(self) -> list[object]:
            started.set()
            assert release.wait(5)
            return []

    worker = CrawlWorkerController(Coordinator())
    reports: list[dict[str, object]] = []
    thread = Thread(target=lambda: reports.append(dict(worker.run_once())))
    thread.start()
    assert started.wait(5)
    busy = worker.run_once()
    release.set()
    thread.join(5)

    assert busy["status"] == "busy"
    assert reports[0]["status"] == "completed"
    assert worker.status()["runs_total"] == 1


def test_cli_worker_modes_are_mutually_exclusive() -> None:
    from nptu_assistant.cli import build_parser

    parser = build_parser()

    args = parser.parse_args(["crawl-worker", "--once", "--dry-run"])
    assert args.command == "crawl-worker"
    assert args.once is True
    assert args.loop is False
    assert args.dry_run is True

    try:
        parser.parse_args(["crawl-worker", "--once", "--loop"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("--once 與 --loop 應互斥")


def test_openapi_documents_admin_crawl_control_paths() -> None:
    client = make_client(RecordingAdmin())
    schema = client.app.openapi()

    assert "/v1/admin/crawl/status" in schema["paths"]
    assert "/v1/admin/crawl/schedule" in schema["paths"]
    assert schema["paths"]["/v1/admin/crawl/schedule"]["post"]["responses"]["202"]
