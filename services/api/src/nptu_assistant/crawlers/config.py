from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from soupsieve import SelectorSyntaxError, compile as compile_selector

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)


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


class SiteSearchScoringWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phrase: float = Field(default=0.16, ge=0, le=1)
    title: float = Field(default=0.14, ge=0, le=1)
    heading: float = Field(default=0.10, ge=0, le=1)
    anchor: float = Field(default=0.10, ge=0, le=1)
    url: float = Field(default=0.05, ge=0, le=1)
    body: float = Field(default=0.08, ge=0, le=1)
    lexical: float = Field(default=0.13, ge=0, le=1)
    semantic: float = Field(default=0.16, ge=0, le=1)
    parent: float = Field(default=0.10, ge=0, le=1)
    discovery: float = Field(default=0.05, ge=0, le=1)
    depth_penalty: float = Field(default=0.03, ge=0, le=0.25)


class SiteSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    enabled: bool = False
    name: str = "nptu-domain-search"
    seed_urls: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    max_pages: int = Field(default=40, ge=1, le=200)
    max_items: int = Field(default=20, ge=1, le=20)
    max_candidate_urls: int = Field(default=80, ge=1, le=500)
    max_depth: int = Field(default=3, ge=0, le=8)
    max_pages_per_host: int = Field(default=30, ge=1, le=200)
    request_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    query_timeout_seconds: float = Field(default=25.0, ge=1.0, le=120.0)
    max_response_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1024, le=8 * 1024 * 1024
    )
    embedding_batch_size: int = Field(default=32, ge=1, le=128)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    relevance_threshold: float = Field(default=0.18, ge=0, le=1)
    high_confidence_score: float = Field(default=0.50, ge=0, le=1)
    failure_warning_margin: float = Field(default=0.05, ge=0, le=1)
    early_stop_min_results: int = Field(default=4, ge=1, le=20)
    database_min_score: float = Field(default=0.58, ge=0, le=1)
    database_min_results: int = Field(default=2, ge=1, le=20)
    database_min_content_chars: int = Field(default=160, ge=1, le=10_000)
    weights: SiteSearchScoringWeights = Field(default_factory=SiteSearchScoringWeights)
    unit: str = "國立屏東大學"
    category: str = "NPTU 網域搜尋"

    @field_validator("name", "unit", "category")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        if not value:
            raise ValueError("網站搜尋設定文字不得為空")
        return value

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
            raise ValueError("網站搜尋 allowed host 必須是 NPTU 官方網域")
        if len(normalized) != len(set(normalized)):
            raise ValueError("網站搜尋 allowed host 不可重複")
        return normalized

    @model_validator(mode="after")
    def validate_scope(self) -> "SiteSearchConfig":
        if not self.enabled:
            return self
        if not self.seed_urls:
            raise ValueError("啟用網站搜尋時必須設定至少一個 seed URL")
        if not self.allowed_hosts:
            raise ValueError("啟用網站搜尋時必須設定至少一個 allowed host")
        normalized_urls: list[str] = []
        for url in self.seed_urls:
            try:
                canonical_url = canonicalize_nptu_url(url)
            except ValueError as exc:
                raise ValueError(
                    "網站搜尋 seed URL 必須是 NPTU 官方 HTTPS 網址"
                ) from exc
            if not is_allowed_source_url(canonical_url, self.allowed_hosts):
                raise ValueError("網站搜尋 seed URL 不在 allowed host 範圍內")
            normalized_urls.append(canonical_url)
        self.seed_urls = list(dict.fromkeys(normalized_urls))
        if self.high_confidence_score < self.relevance_threshold:
            raise ValueError("高可信度門檻不得低於相關性門檻")
        if self.max_candidate_urls < self.max_items:
            raise ValueError("候選 URL 上限不得低於回傳結果上限")
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
    site_search: SiteSearchConfig | None = None

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
        if any(
            not alias.strip() or not canonical.strip()
            for alias, canonical in value.items()
        ):
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
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise ValueError("crawler 設定必須包含 sources list")
    configs = [CrawlerSourceConfig.model_validate(item) for item in sources]
    if len({item.name for item in configs}) != len(configs):
        raise ValueError("crawler source name 不可重複")
    return configs


def load_keyword_search_config(path: Path) -> KeywordSearchConfig:
    payload = _load_payload(path)
    if not isinstance(payload.get("keyword_search"), dict):
        raise ValueError("crawler 設定必須包含 keyword_search mapping")
    return KeywordSearchConfig.model_validate(payload["keyword_search"])
