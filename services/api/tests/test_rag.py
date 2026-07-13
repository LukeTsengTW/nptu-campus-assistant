from __future__ import annotations

from nptu_assistant.api.schemas import Confidence
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
