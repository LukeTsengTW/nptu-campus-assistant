from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_code_review_graph_uses_embeddings_then_graph_then_text_fallback() -> None:
    instructions = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "必須先使用 `code-review-graph` MCP 的 embeddings 語意搜尋" in instructions
    assert "優先使用 `get_minimal_context_tool`" in instructions
    assert "不得把空結果視為已找到答案" in instructions
    assert "`rg` / `rg --files`" in instructions


def test_code_review_graph_runtime_data_is_ignored() -> None:
    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".code-review-graph/" in gitignore.splitlines()
