from __future__ import annotations

import argparse
import asyncio
import json
import signal
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any, cast

from nptu_assistant.core.settings import (
    WORKSPACE_ROOT,
    get_settings,
    resolve_workspace_path,
)
from nptu_assistant.crawlers.config import load_source_configs
from nptu_assistant.db.repositories import get_or_create_source
from nptu_assistant.main import create_app
from nptu_assistant.wiring import build_services


@contextmanager
def _install_stop_handlers(callback: Callable[[], None]) -> Iterator[None]:
    """將 SIGINT/SIGTERM 轉成可測試且可恢復的 graceful-stop callback。"""

    previous: dict[int, Any] = {}

    def handle_signal(_signum: int, _frame: Any) -> None:
        callback()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        except ValueError:
            # signal handlers can only be installed from the main thread.
            continue
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NPTU 校務資訊助理管理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="建立可重複的官方來源 seed")
    subparsers.add_parser("ingest-documents", help="匯入固定資料目錄的官方文件")
    crawl = subparsers.add_parser("crawl-announcements", help="爬取設定檔中的公告來源")
    crawl.add_argument("--source", action="append", dest="sources")
    worker = subparsers.add_parser("crawl-worker", help="執行公告刷新 worker")
    mode = worker.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="只執行一次刷新")
    mode.add_argument("--loop", action="store_true", help="持續執行刷新迴圈")
    worker.add_argument(
        "--dry-run", action="store_true", help="只輸出預計執行的 metrics"
    )
    site_map_worker = subparsers.add_parser(
        "site-map-crawl", help="執行持久化網頁地圖增量 crawler"
    )
    site_map_mode = site_map_worker.add_mutually_exclusive_group()
    site_map_mode.add_argument("--once", action="store_true")
    site_map_mode.add_argument("--loop", action="store_true")
    site_map_worker.add_argument("--worker-id", default=None)
    site_map_worker.add_argument("--batch-size", type=int, default=4)
    site_map_worker.add_argument("--max-pages", type=int, default=None)
    site_map_worker.add_argument("--max-duration-seconds", type=float, default=None)
    site_map_worker.add_argument("--poll-interval-seconds", type=float, default=60.0)
    site_map_worker.add_argument("--concurrency", type=int, default=None)
    site_map_worker.add_argument("--lease-seconds", type=float, default=300.0)
    site_map_worker.add_argument("--dry-run", action="store_true")
    export = subparsers.add_parser("export-openapi", help="輸出 OpenAPI schema")
    export.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    services = build_services(settings)
    if args.command == "seed":
        factory = cast(Any, services["session_factory"])
        configs = load_source_configs(
            resolve_workspace_path(settings.crawler_config_path)
        )
        with factory.begin() as session:
            for config in configs:
                if config.adapter == "fixture":
                    continue
                get_or_create_source(
                    session,
                    name=config.name,
                    base_url=config.url,
                    unit=config.unit,
                    source_type="announcement",
                    crawl_enabled=config.enabled,
                    crawl_interval_minutes=config.crawl_interval_minutes,
                )
        print(
            json.dumps(
                {"seeded": len([item for item in configs if item.adapter != "fixture"])}
            )
        )
        return 0
    if args.command == "ingest-documents":
        summary = cast(Any, services["ingestion_service"]).run()
        print(summary.model_dump_json())
        return 1 if summary.failed else 0
    if args.command == "crawl-announcements":
        summary = cast(Any, services["crawler_service"]).run(args.sources)
        print(summary.model_dump_json())
        return 1 if summary.failed else 0
    if args.command == "crawl-worker":
        worker = cast(Any, services["refresh_scheduler"])
        if args.loop:
            with _install_stop_handlers(worker.stop):
                asyncio.run(worker.run_loop(dry_run=args.dry_run))
            return 0
        report = worker.run_once(dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, default=str))
        return 1 if report.get("status") == "failed" else 0
    if args.command == "site-map-crawl":
        worker = cast(Any, services["incremental_crawler"])
        worker.configure_runtime(
            worker_id=args.worker_id,
            max_concurrency=args.concurrency,
            lease_duration=timedelta(seconds=args.lease_seconds),
        )
        if args.dry_run:
            store = cast(Any, services["crawl_lease_repository"])
            print(json.dumps({"dry_run": True, **store.status()}, default=str))
            return 0
        if args.loop:
            stop_event = threading.Event()
            with _install_stop_handlers(stop_event.set):
                worker.run_loop(
                    poll_interval_seconds=args.poll_interval_seconds,
                    max_pages=args.max_pages,
                    max_duration_seconds=args.max_duration_seconds,
                    batch_size=args.batch_size,
                    stop_event=stop_event,
                )
            return 0
        result = worker.run_once(batch_size=args.batch_size)
        print(
            json.dumps(
                {
                    "results": len(result.results),
                    "counts": {str(key): value for key, value in result.counts.items()},
                },
                ensure_ascii=False,
                default=str,
            )
        )
        return 0
    output = args.output or WORKSPACE_ROOT / "packages/shared/openapi.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            create_app(settings=settings).openapi(), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(json.dumps({"openapi": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
