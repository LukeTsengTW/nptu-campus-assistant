from __future__ import annotations

from pathlib import Path

import yaml

from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.parsers import parse_document


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = WORKSPACE_ROOT / "data/official-documents/nptu-academic-units.md"
SIDECAR = WORKSPACE_ROOT / "data/official-documents/nptu-academic-units.yaml"


def test_academic_units_knowledge_document_has_official_metadata_and_departments():
    metadata = DocumentMetadata.model_validate(
        yaml.safe_load(SIDECAR.read_text(encoding="utf-8"))
    )
    text = parse_document(DOCUMENT)

    assert str(metadata.source_url) == "https://www.nptu.edu.tw/p/412-1000-2972.php?Lang=zh-tw"
    assert metadata.document_type == "academic_units_snapshot"
    for college in (
        "管理學院",
        "資訊學院",
        "教育學院",
        "人文社會學院",
        "理學院",
        "國際學院",
        "大武山學院",
    ):
        assert college in text
    for department in (
        "電腦科學與人工智慧學系",
        "電腦與通訊學系",
        "資訊工程學系",
        "智慧機器人學系",
        "應用數學系",
        "體育學系",
    ):
        assert department in text
