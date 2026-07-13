from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

from nptu_assistant.core.security import is_allowed_nptu_url


class CrawlerSourceConfig(BaseModel):
    name: str
    adapter: str
    url: str
    unit: str
    category: str | None = None
    enabled: bool = True
    crawl_interval_minutes: int = Field(default=60, ge=1)
    max_items: int = Field(default=50, ge=1, le=200)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str, info: object) -> str:
        adapter = getattr(info, "data", {}).get("adapter")
        if adapter != "fixture" and not is_allowed_nptu_url(value):
            raise ValueError("crawler URL 必須是 NPTU 官方 HTTPS 網址")
        return value


class KeywordSearchConfig(BaseModel):
    name: str
    session_url: str
    bootstrap_url: str
    bootstrap_method: Literal["get", "post"] = "post"
    url: str
    search_types: list[Literal["part", "com"]]
    max_items: int = Field(default=20, ge=1, le=200)
    unit: str
    category: str
    aliases: dict[str, str] = Field(default_factory=dict)
    crawl_interval_minutes: int = Field(default=60, ge=1)

    @field_validator("session_url", "bootstrap_url", "url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not is_allowed_nptu_url(value):
            raise ValueError("關鍵字搜尋 URL 必須是 NPTU 官方 HTTPS 網址")
        return value

    @field_validator("search_types")
    @classmethod
    def validate_search_types(cls, value: list[str]) -> list[str]:
        if not value or len(value) != len(set(value)):
            raise ValueError("search_types 必須包含不重複的搜尋分類")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not alias.strip() or not canonical.strip() for alias, canonical in value.items()):
            raise ValueError("搜尋別名與完整名稱不得為空")
        return {alias.strip(): canonical.strip() for alias, canonical in value.items()}


def _load_payload(path: Path) -> dict[str, object]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("crawler 設定必須是 mapping")
    return payload


def load_source_configs(path: Path) -> list[CrawlerSourceConfig]:
    payload = _load_payload(path)
    if not isinstance(payload.get("sources"), list):
        raise ValueError("crawler 設定必須包含 sources list")
    configs = [CrawlerSourceConfig.model_validate(item) for item in payload["sources"]]
    if len({item.name for item in configs}) != len(configs):
        raise ValueError("crawler source name 不可重複")
    return configs


def load_keyword_search_config(path: Path) -> KeywordSearchConfig:
    payload = _load_payload(path)
    if not isinstance(payload.get("keyword_search"), dict):
        raise ValueError("crawler 設定必須包含 keyword_search mapping")
    return KeywordSearchConfig.model_validate(payload["keyword_search"])
