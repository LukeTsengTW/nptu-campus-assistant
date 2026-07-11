from __future__ import annotations

from pathlib import Path

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


def load_source_configs(path: Path) -> list[CrawlerSourceConfig]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise ValueError("crawler 設定必須包含 sources list")
    configs = [CrawlerSourceConfig.model_validate(item) for item in payload["sources"]]
    if len({item.name for item in configs}) != len(configs):
        raise ValueError("crawler source name 不可重複")
    return configs
