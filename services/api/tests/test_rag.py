from __future__ import annotations

from nptu_assistant.api.schemas import Confidence
from nptu_assistant.rag.prompts import SYSTEM_INSTRUCTIONS
from nptu_assistant.rag.service import confidence_for_score, sanitize_user_facing_text


def test_confidence_thresholds() -> None:
    assert confidence_for_score(0.8) is Confidence.HIGH
    assert confidence_for_score(0.6) is Confidence.MEDIUM
    assert confidence_for_score(0.4) is Confidence.LOW


def test_user_facing_sanitizer_keeps_only_allowlisted_urls() -> None:
    allowed = "https://www.nptu.edu.tw/official"
    answer = sanitize_user_facing_text(
        f"正式 {allowed}，偽造 https://example.com/x。",
        allowed_urls={allowed},
    )

    assert allowed in answer
    assert "example.com" not in answer


def test_system_instructions_define_department_aliases_without_electrical_department_ambiguity():
    assert "詢問科系、學院、學程或中心名稱時，使用 search_documents" in SYSTEM_INSTRUCTIONS
    assert "電科系＝電腦科學與人工智慧學系" in SYSTEM_INSTRUCTIONS
    assert "不得解讀為電腦與通訊學系" in SYSTEM_INSTRUCTIONS
    assert "資工系＝資訊工程學系" in SYSTEM_INSTRUCTIONS
    assert "機器人系、智機系＝智慧機器人學系" in SYSTEM_INSTRUCTIONS
    assert "英語系、英文系＝英語學系" in SYSTEM_INSTRUCTIONS


def test_system_instructions_define_administrative_unit_aliases() -> None:
    assert "詢問行政單位、處室、組、中心或室名稱時，使用 search_documents" in SYSTEM_INSTRUCTIONS
    assert "計網中心＝計算機與網路中心" in SYSTEM_INSTRUCTIONS
    assert "校友組＝校友服務組" in SYSTEM_INSTRUCTIONS
    assert "育成中心＝創新育成中心" in SYSTEM_INSTRUCTIONS
    assert "國文組、外生組＝國際學生組" in SYSTEM_INSTRUCTIONS
    assert "推廣中心＝推廣教育中心" in SYSTEM_INSTRUCTIONS
