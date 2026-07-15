from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_code_review_graph_is_project_first_with_traditional_search_fallback() -> None:
    instructions = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "必須先使用 `code-review-graph` MCP 工具" in instructions
    assert "查詢結果為空" in instructions
    assert "`rg` / `rg --files`" in instructions


def test_code_review_graph_runtime_data_is_ignored() -> None:
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".code-review-graph/" in gitignore.splitlines()
