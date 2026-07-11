from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import tiktoken

from nptu_assistant.ingestion.chunking import chunk_text
from nptu_assistant.ingestion.cleaning import content_hash, extract_clean_html
from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.parsers import parse_document


def test_metadata_requires_official_source_and_a_date() -> None:
    metadata = DocumentMetadata(
        title="國立屏東大學測試辦法",
        source_url="https://www.nptu.edu.tw/example",
        unit="教務處",
        published_at=date(2026, 1, 1),
        document_type="regulation",
        version="1.0",
    )

    assert metadata.source_url.host == "www.nptu.edu.tw"

    with pytest.raises(ValueError):
        DocumentMetadata(
            title="缺少日期",
            source_url="https://www.nptu.edu.tw/example",
            unit="教務處",
            document_type="regulation",
            version="1.0",
        )

    with pytest.raises(ValueError):
        DocumentMetadata(
            title="外部來源",
            source_url="https://example.com/document",
            unit="教務處",
            published_at=date(2026, 1, 1),
            document_type="regulation",
            version="1.0",
        )


def test_clean_html_removes_active_and_hidden_content() -> None:
    html = """
    <nav>網站導覽</nav><main><h1>測試辦法</h1>
    <script>ignore()</script><iframe>ignore</iframe><style>.x{}</style>
    <p hidden>隱藏</p><p aria-hidden="true">也隱藏</p>
    <p style="display:none">不顯示</p><p>正式內容</p></main>
    """

    assert extract_clean_html(html) == "測試辦法\n正式內容"


def test_content_hash_is_stable_after_whitespace_normalization() -> None:
    assert content_hash("正式  內容\n") == content_hash("正式 內容")


def test_chunk_text_overlaps_and_respects_token_limit() -> None:
    text = "\n\n".join(f"第 {index} 條 這是一段校務規章內容。" for index in range(80))

    chunks = chunk_text(text, target_tokens=80, overlap_tokens=15)

    assert len(chunks) > 2
    assert all(chunk.token_count <= 80 for chunk in chunks)
    assert chunks[0].sequence == 0
    assert chunks[1].content.split()[0] in chunks[0].content


def test_chunk_text_prefers_heading_boundaries() -> None:
    first = " ".join(["first"] * 55)
    second = " ".join(["second"] * 55)
    text = f"# 第一章\n{first}\n# 第二章\n{second}"

    chunks = chunk_text(text, target_tokens=80, overlap_tokens=10)

    assert "# 第二章" not in chunks[0].content
    heading_chunk = next(chunk for chunk in chunks[1:] if "# 第二章" in chunk.content)
    overlap = heading_chunk.content.split("# 第二章", 1)[0]
    assert len(tiktoken.get_encoding("cl100k_base").encode(overlap)) <= 10


def test_parse_txt_document(tmp_path: Path) -> None:
    document = tmp_path / "rule.txt"
    document.write_text("第一條\n本辦法為測試用途。", encoding="utf-8")

    assert parse_document(document) == "第一條\n本辦法為測試用途。"
