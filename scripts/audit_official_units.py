from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE_ROOT / "services" / "api" / "src"))

from nptu_assistant.crawlers.config import load_source_configs  # noqa: E402
from nptu_assistant.crawlers.official_units import (  # noqa: E402
    AnnouncementStrategy,
    UnitStatus,
    load_official_unit_directory,
)


DEFAULT_DIRECTORY = WORKSPACE_ROOT / "data" / "sources" / "official_units.yaml"
DEFAULT_SOURCES = WORKSPACE_ROOT / "data" / "sources" / "announcements.yaml"


def audit(directory_path: Path, sources_path: Path) -> dict[str, object]:
    directory = load_official_unit_directory(directory_path)
    sources = {source.name: source for source in load_source_configs(sources_path)}
    issues: list[str] = []
    rows: list[dict[str, object]] = []
    for unit in directory.units:
        if unit.enabled and not unit.homepage_url and not unit.unsupported_reason:
            issues.append(f"{unit.canonical_name}: 缺少 homepage 或 unsupported reason")
        if unit.status is UnitStatus.DISCONTINUED and unit.enabled:
            issues.append(f"{unit.canonical_name}: 停招單位不得 enabled")
        if unit.announcement_strategy is AnnouncementStrategy.CONFIGURED_LISTING:
            source = sources.get(unit.announcement_source_name or "")
            if source is None:
                issues.append(f"{unit.canonical_name}: configured source 不存在")
            elif source.unit != unit.canonical_name:
                issues.append(f"{unit.canonical_name}: configured source unit 不一致")
        rows.append(
            {
                "canonical_name": unit.canonical_name,
                "enabled": unit.enabled,
                "status": unit.status.value,
                "homepage_url": unit.homepage_url,
                "allowed_hosts": list(unit.allowed_hosts),
                "announcement_strategy": unit.announcement_strategy.value,
                "unsupported_reason": unit.unsupported_reason,
            }
        )
    counts = {
        "total": len(directory.units),
        "active": len(directory.active_units),
        "stopped": sum(
            unit.status is UnitStatus.DISCONTINUED for unit in directory.units
        ),
        "homepage": sum(bool(unit.homepage_url) for unit in directory.units),
        **{
            strategy.value: sum(
                unit.announcement_strategy is strategy for unit in directory.units
            )
            for strategy in AnnouncementStrategy
        },
    }
    return {
        "source_url": directory.source_url,
        "verified_at": directory.verified_at,
        "valid": not issues,
        "counts": counts,
        "issues": issues,
        "units": rows,
    }


def to_markdown(report: dict[str, object]) -> str:
    counts = report["counts"]
    assert isinstance(counts, dict)
    issues = report["issues"]
    assert isinstance(issues, list)
    units = report["units"]
    assert isinstance(units, list)
    lines = [
        "# 官方單位目錄稽核",
        "",
        f"- 驗證：{'通過' if report['valid'] else '失敗'}",
        f"- 官方來源：{report['source_url']}",
        f"- 驗證日期：{report['verified_at']}",
        f"- 總數：{counts['total']}",
        f"- 啟用：{counts['active']}",
        f"- 停招：{counts['stopped']}",
        f"- 官方首頁：{counts['homepage']}",
        f"- 固定公告列表：{counts['configured_listing']}",
        f"- 單位網站搜尋：{counts['scoped_site_search']}",
        f"- 不支援：{counts['unsupported']}",
        "",
        "## 問題",
        "",
        *(f"- {issue}" for issue in issues),
    ]
    if not issues:
        lines.append("- 無")
    lines.extend(
        [
            "",
            "## 單位",
            "",
            "| 單位 | 啟用 | 狀態 | 官方首頁 | 公告策略 |",
            "|---|---:|---|---|---|",
        ]
    )
    for raw in units:
        assert isinstance(raw, dict)
        lines.append(
            "| {canonical_name} | {enabled} | {status} | {homepage_url} | "
            "{announcement_strategy} |".format(**raw)
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="稽核官方學術單位目錄")
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = audit(args.directory, args.sources)
    content = (
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.format == "json"
        else to_markdown(report)
    )
    if args.output:
        args.output.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
