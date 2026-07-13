from __future__ import annotations

from pathlib import Path

import yaml

from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.parsers import parse_document


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = WORKSPACE_ROOT / "data/official-documents/nptu-administrative-units.md"
SIDECAR = WORKSPACE_ROOT / "data/official-documents/nptu-administrative-units.yaml"


def test_administrative_units_knowledge_document_has_official_metadata_and_units():
    metadata = DocumentMetadata.model_validate(
        yaml.safe_load(SIDECAR.read_text(encoding="utf-8"))
    )
    text = parse_document(DOCUMENT)

    assert str(metadata.source_url) == "https://www.nptu.edu.tw/p/412-1000-86.php?Lang=zh-tw"
    assert metadata.document_type == "administrative_units_snapshot"
    for office in (
        "校長室",
        "秘書室",
        "教務處",
        "學務處",
        "總務處",
        "研究發展處",
        "國際事務處",
        "職涯發展暨教育推廣處",
        "主計室",
        "人事室",
        "圖書館",
        "計算機與網路中心",
        "體育室",
    ):
        assert office in text
    for unit in (
        "行政法制組",
        "生活輔導組",
        "衛生保健組",
        "軍訓暨校安中心",
        "創新育成中心",
        "國際學生組",
        "推廣教育中心",
        "競賽活動組",
    ):
        assert unit in text
