from __future__ import annotations

import argparse
import json
from pathlib import Path

from nptu_assistant.core.settings import WORKSPACE_ROOT, get_settings, resolve_workspace_path
from nptu_assistant.crawlers.config import load_source_configs
from nptu_assistant.db.repositories import get_or_create_source
from nptu_assistant.main import create_app
from nptu_assistant.wiring import build_services


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NPTU 校務資訊助理管理工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="建立可重複的官方來源 seed")
    subparsers.add_parser("ingest-documents", help="匯入固定資料目錄的官方文件")
    crawl = subparsers.add_parser("crawl-announcements", help="爬取設定檔中的公告來源")
    crawl.add_argument("--source", action="append", dest="sources")
    export = subparsers.add_parser("export-openapi", help="輸出 OpenAPI schema")
    export.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    services = build_services(settings)
    if args.command == "seed":
        factory = services["session_factory"]
        configs = load_source_configs(resolve_workspace_path(settings.crawler_config_path))
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
        print(json.dumps({"seeded": len([item for item in configs if item.adapter != "fixture"])}))
        return 0
    if args.command == "ingest-documents":
        summary = services["ingestion_service"].run()
        print(summary.model_dump_json())
        return 1 if summary.failed else 0
    if args.command == "crawl-announcements":
        summary = services["crawler_service"].run(args.sources)
        print(summary.model_dump_json())
        return 1 if summary.failed else 0
    output = args.output or WORKSPACE_ROOT / "packages/shared/openapi.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(create_app(settings=settings).openapi(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"openapi": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
