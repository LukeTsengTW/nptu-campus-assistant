from __future__ import annotations

import pytest
from pydantic import ValidationError

from nptu_assistant.api.schemas import SourceReference
from nptu_assistant.core.logging import redact_sensitive_fields
from nptu_assistant.core.rate_limit import InMemoryRateLimiter
from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)
from nptu_assistant.core.settings import Settings, WORKSPACE_ROOT, resolve_workspace_path


def test_settings_parse_cors_without_wildcard() -> None:
    settings = Settings(
        _env_file=None,
        cors_allowed_origins="http://127.0.0.1:3000,http://localhost:3000",
    )

    assert settings.cors_origins == [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]


def test_workspace_path_resolution_is_independent_of_cwd(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert resolve_workspace_path("data/sources/announcements.yaml") == (
        WORKSPACE_ROOT / "data/sources/announcements.yaml"
    )


def test_admin_api_defaults_to_disabled_outside_development() -> None:
    production = Settings(_env_file=None, app_env="production", admin_api_enabled=None)
    development = Settings(_env_file=None, app_env="development", admin_api_enabled=None)

    assert production.is_admin_enabled is False
    assert development.is_admin_enabled is True


def test_logging_redacts_sensitive_fields_recursively() -> None:
    payload = {
        "request_id": "req-1",
        "question": "我的問題全文",
        "nested": {"OPENAI_API_KEY": "secret", "safe": "ok"},
    }

    assert redact_sensitive_fields(payload) == {
        "request_id": "req-1",
        "question": "[REDACTED]",
        "nested": {"OPENAI_API_KEY": "[REDACTED]", "safe": "ok"},
    }


def test_settings_reject_wildcard_cors() -> None:
    with pytest.raises(ValueError, match="萬用字元"):
        Settings(_env_file=None, cors_allowed_origins="*")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.nptu.edu.tw/p/1", True),
        ("https://nptu.edu.tw/", True),
        ("https://evil-nptu.edu.tw/", False),
        ("http://www.nptu.edu.tw/", False),
        ("https://nptu.edu.tw.evil.example/", False),
        ("https://user@nptu.edu.tw/", False),
        ("https://nptu.edu.tw:8443/", False),
        ("https://nptu.edu.tw:443/", True),
    ],
)
def test_nptu_url_allowlist(url: str, allowed: bool) -> None:
    assert is_allowed_nptu_url(url) is allowed


def test_source_url_allowlist_uses_domain_boundaries() -> None:
    allowed_hosts = ["ccs.nptu.edu.tw"]

    assert is_allowed_source_url("https://ccs.nptu.edu.tw/index.php", allowed_hosts)
    assert is_allowed_source_url("https://news.ccs.nptu.edu.tw/item", allowed_hosts)
    assert not is_allowed_source_url("https://www.nptu.edu.tw/item", allowed_hosts)
    assert not is_allowed_source_url("https://evilccs.nptu.edu.tw/item", allowed_hosts)


def test_canonicalize_nptu_url_removes_fragment_and_default_port() -> None:
    assert canonicalize_nptu_url(
        "https://CCS.NPTU.EDU.TW:443/p/406.php?Lang=zh-tw#content"
    ) == "https://ccs.nptu.edu.tw/p/406.php?Lang=zh-tw"

    with pytest.raises(ValueError, match="NPTU"):
        canonicalize_nptu_url("https://example.com/item")


def test_rate_limiter_blocks_after_limit() -> None:
    limiter = InMemoryRateLimiter(clock=lambda: 100.0)

    assert limiter.allow("chat", "127.0.0.1", limit=2, window_seconds=60)
    assert limiter.allow("chat", "127.0.0.1", limit=2, window_seconds=60)
    assert not limiter.allow("chat", "127.0.0.1", limit=2, window_seconds=60)


def test_source_reference_only_accepts_official_nptu_sources() -> None:
    with pytest.raises(ValidationError):
        SourceReference(
            title="外部來源",
            url="https://www.nptu.edu.tw/example",
            unit="測試",
            published_at=None,
            source_type="community",
        )
