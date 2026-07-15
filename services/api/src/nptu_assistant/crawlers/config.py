from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from soupsieve import SelectorSyntaxError, compile as compile_selector

from nptu_assistant.core.security import is_allowed_nptu_url, is_allowed_source_url


class HtmlListingSelectors(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    listing: str
    item: str
    date: str
    title_link: str
    link_attribute: str = "href"

    @field_validator("listing", "item", "date", "title_link")
    @classmethod
    def validate_selector(cls, value: str) -> str:
        if not value:
            raise ValueError("CSS selector 不得為空")
        try:
            compile_selector(value)
        except SelectorSyntaxError as exc:
            raise ValueError(f"CSS selector 語法錯誤：{value}") from exc
        return value

    @field_validator("link_attribute")
    @classmethod
    def validate_link_attribute(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_.:-]*", value):
            raise ValueError("link attribute 名稱不合法")
        return value


class DetailPageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    content_selector: str | None = None

    @field_validator("content_selector")
    @classmethod
    def validate_content_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value:
            raise ValueError("detail content selector 不得為空")
        try:
            compile_selector(value)
        except SelectorSyntaxError as exc:
            raise ValueError(f"CSS selector 語法錯誤：{value}") from exc
        return value


class DynamicListingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    url: str
    method: Literal["get", "post"] = "post"
    wrapper_id: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        if not is_allowed_nptu_url(value):
            raise ValueError("動態公告列表 URL 必須是 NPTU 官方 HTTPS 網址")
        return value

    @field_validator("wrapper_id")
    @classmethod
    def validate_wrapper_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", value):
            raise ValueError("動態公告列表 wrapper id 不合法")
        return value


class CrawlerSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str
    adapter: str
    url: str
    unit: str
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    enabled: bool = True
    crawl_interval_minutes: int = Field(default=60, ge=1)
    max_items: int = Field(default=50, ge=1, le=200)
    allowed_hosts: list[str] = Field(default_factory=list)
    selectors: HtmlListingSelectors | None = None
    detail: DetailPageConfig | None = None
    dynamic_listing: DynamicListingConfig | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str, info: object) -> str:
        adapter = getattr(info, "data", {}).get("adapter")
        if adapter != "fixture" and not is_allowed_nptu_url(value):
            raise ValueError("crawler URL 必須是 NPTU 官方 HTTPS 網址")
        return value

    @field_validator("name", "unit")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value:
            raise ValueError("source name 與 unit 不得為空")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        normalized = [alias.strip() for alias in value]
        if any(not alias for alias in normalized):
            raise ValueError("來源別名不得為空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("來源別名不可重複")
        return normalized

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, value: list[str]) -> list[str]:
        normalized = [host.strip().lower().rstrip(".") for host in value]
        if any(
            not host
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or not is_allowed_nptu_url(f"https://{host}/")
            for host in normalized
        ):
            raise ValueError("allowed host 必須是 NPTU 官方網域")
        if len(normalized) != len(set(normalized)):
            raise ValueError("allowed host 不可重複")
        return normalized

    @model_validator(mode="after")
    def validate_html_source(self) -> "CrawlerSourceConfig":
        if self.adapter != "nptu_html_list":
            return self
        if not self.aliases:
            raise ValueError("HTML 單位來源必須設定至少一個別名")
        if not self.allowed_hosts:
            raise ValueError("HTML 單位來源必須設定 allowed hosts")
        if self.selectors is None:
            raise ValueError("HTML 單位來源必須設定 selectors")
        if not is_allowed_source_url(self.url, self.allowed_hosts):
            raise ValueError("來源 URL host 不在來源 allowlist")
        if self.dynamic_listing is not None:
            if not is_allowed_source_url(self.dynamic_listing.url, self.allowed_hosts):
                raise ValueError("動態公告列表 URL host 不在來源 allowlist")
            if self.selectors.listing != f"#{self.dynamic_listing.wrapper_id}":
                raise ValueError("動態公告列表 wrapper id 必須對應 listing selector")
        return self


class KeywordSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

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
    source_routes: dict[str, str] = Field(default_factory=dict)
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

    @field_validator("source_routes")
    @classmethod
    def validate_source_routes(cls, value: dict[str, str]) -> dict[str, str]:
        routes: dict[str, str] = {}
        for keyword, source_name in value.items():
            normalized_keyword = keyword.strip()
            normalized_source_name = source_name.strip()
            if not normalized_keyword or not normalized_source_name:
                raise ValueError("公告來源路由關鍵字與來源名稱不得為空")
            if normalized_keyword in routes:
                raise ValueError("公告來源路由關鍵字不可重複")
            routes[normalized_keyword] = normalized_source_name
        return routes


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
