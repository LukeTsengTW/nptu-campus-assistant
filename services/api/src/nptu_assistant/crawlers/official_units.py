from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
import re
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)
from nptu_assistant.crawlers.aliases import AliasNormalizer
from nptu_assistant.crawlers.config import HtmlListingSelectors


DEFAULT_OFFICIAL_UNITS_PATH = (
    Path(__file__).resolve().parents[5] / "data" / "sources" / "official_units.yaml"
)


class UnitType(StrEnum):
    COLLEGE = "college"
    DEPARTMENT = "department"
    INSTITUTE = "institute"
    DEGREE_PROGRAM = "degree_program"
    ACADEMIC_CENTER = "academic_center"
    GROUP = "group"


class UnitStatus(StrEnum):
    ACTIVE = "active"
    DISCONTINUED = "discontinued"


class AnnouncementStrategy(StrEnum):
    CONFIGURED_LISTING = "configured_listing"
    SCOPED_SITE_SEARCH = "scoped_site_search"
    UNSUPPORTED = "unsupported"


class UnitSiteSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    seed_urls: list[str] = Field(default_factory=list)
    preferred_hosts: list[str] = Field(default_factory=list)


class UnitAnnouncementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    strategy: AnnouncementStrategy
    source_name: str | None = None
    listing_url: str | None = None
    adapter: str | None = None
    selectors: HtmlListingSelectors | None = None
    detail_enabled: bool = False
    crawl_interval_minutes: int = Field(default=60, ge=1)
    max_items: int = Field(default=20, ge=1, le=200)


class OfficialUnitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    unit_type: UnitType
    parent_unit: str | None = None
    enabled: bool = True
    status: UnitStatus = UnitStatus.ACTIVE
    homepage_url: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    site_search: UnitSiteSearchConfig = Field(default_factory=UnitSiteSearchConfig)
    announcements: UnitAnnouncementConfig
    unsupported_reason: str | None = None

    @field_validator("canonical_name")
    @classmethod
    def validate_canonical_name(cls, value: str) -> str:
        if not value:
            raise ValueError("單位正式名稱不得為空")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("單位 alias 不得為空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("同一單位的 alias 不可重複")
        return normalized

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().lower().rstrip(".") for value in values]
        if any(
            not value
            or not re.fullmatch(r"[a-z0-9.-]+", value)
            or not is_allowed_nptu_url(f"https://{value}/")
            for value in normalized
        ):
            raise ValueError("單位 allowed host 必須是 NPTU 官方網域")
        if len(normalized) != len(set(normalized)):
            raise ValueError("單位 allowed host 不可重複")
        return normalized

    @model_validator(mode="after")
    def validate_capabilities(self) -> "OfficialUnitConfig":
        if self.status is UnitStatus.DISCONTINUED and self.enabled:
            raise ValueError("停招單位不得啟用")
        if self.enabled and not self.homepage_url and not self.unsupported_reason:
            raise ValueError("啟用單位必須設定 homepage 或 unsupported reason")

        if self.homepage_url:
            try:
                self.homepage_url = canonicalize_nptu_url(self.homepage_url)
            except ValueError as exc:
                raise ValueError("單位 homepage 必須是 NPTU 官方 HTTPS 網址") from exc
            homepage_host = (urlsplit(self.homepage_url).hostname or "").lower()
            if homepage_host not in self.allowed_hosts:
                raise ValueError("單位 allowed hosts 必須包含 homepage host")

        normalized_seeds: list[str] = []
        for seed_url in self.site_search.seed_urls:
            try:
                canonical_url = canonicalize_nptu_url(seed_url)
            except ValueError as exc:
                raise ValueError("單位 seed URL 必須是 NPTU 官方 HTTPS 網址") from exc
            if not is_allowed_source_url(canonical_url, self.allowed_hosts):
                raise ValueError("單位 seed URL 不在 allowed host 範圍內")
            normalized_seeds.append(canonical_url)
        self.site_search.seed_urls = list(dict.fromkeys(normalized_seeds))
        preferred = [
            host.lower().rstrip(".") for host in self.site_search.preferred_hosts
        ]
        if any(host not in self.allowed_hosts for host in preferred):
            raise ValueError("preferred host 必須位於單位 allowlist")
        self.site_search.preferred_hosts = list(dict.fromkeys(preferred))

        strategy = self.announcements.strategy
        if strategy is AnnouncementStrategy.CONFIGURED_LISTING:
            if not all(
                (
                    self.announcements.source_name,
                    self.announcements.listing_url,
                    self.announcements.adapter,
                    self.announcements.selectors,
                )
            ):
                raise ValueError(
                    "configured listing 必須設定 source、URL、adapter、selectors"
                )
            if self.announcements.adapter != "nptu_html_list":
                raise ValueError("configured listing 目前只接受 nptu_html_list")
            if not is_allowed_source_url(
                self.announcements.listing_url or "", self.allowed_hosts
            ):
                raise ValueError("公告列表 URL 不在單位 allowlist")
        elif strategy is AnnouncementStrategy.SCOPED_SITE_SEARCH:
            if (
                not self.homepage_url
                or not self.allowed_hosts
                or not self.site_search.seed_urls
            ):
                raise ValueError(
                    "scoped site search 必須設定 homepage、allowed hosts、seed URLs"
                )
        elif not self.unsupported_reason:
            raise ValueError("unsupported 單位必須記錄原因")
        return self

    def resolved(self) -> "ResolvedOfficialUnit":
        return ResolvedOfficialUnit(
            canonical_name=self.canonical_name,
            aliases=tuple(self.aliases),
            unit_type=self.unit_type,
            parent_unit=self.parent_unit,
            enabled=self.enabled,
            status=self.status,
            homepage_url=self.homepage_url,
            allowed_hosts=tuple(self.allowed_hosts),
            seed_urls=tuple(self.site_search.seed_urls),
            preferred_hosts=tuple(self.site_search.preferred_hosts),
            announcement_strategy=self.announcements.strategy,
            announcement_source_name=self.announcements.source_name,
            unsupported_reason=self.unsupported_reason,
        )


@dataclass(frozen=True, slots=True)
class ResolvedOfficialUnit:
    canonical_name: str
    aliases: tuple[str, ...]
    unit_type: UnitType
    parent_unit: str | None
    enabled: bool
    status: UnitStatus
    homepage_url: str | None
    allowed_hosts: tuple[str, ...]
    seed_urls: tuple[str, ...]
    preferred_hosts: tuple[str, ...]
    announcement_strategy: AnnouncementStrategy
    announcement_source_name: str | None
    unsupported_reason: str | None


@dataclass(frozen=True, slots=True)
class DocumentSearchScope:
    canonical_unit: str | None
    homepage_url: str | None
    preferred_hosts: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    seed_urls: tuple[str, ...]


class OfficialUnitDirectoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str
    verified_at: str
    units: list[OfficialUnitConfig]

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        if not is_allowed_nptu_url(value):
            raise ValueError("單位目錄來源必須是 NPTU 官方 HTTPS 網址")
        return value

    @model_validator(mode="after")
    def validate_registry(self) -> "OfficialUnitDirectoryPayload":
        canonical_names = [unit.canonical_name for unit in self.units]
        if len(canonical_names) != len(set(canonical_names)):
            raise ValueError("單位正式名稱不可重複")
        missing_parents = sorted(
            {
                unit.parent_unit
                for unit in self.units
                if unit.parent_unit and unit.parent_unit not in canonical_names
            }
        )
        if missing_parents:
            raise ValueError("單位 parent 不存在：" + "、".join(missing_parents))
        source_names = [
            unit.announcements.source_name
            for unit in self.units
            if unit.announcements.source_name
        ]
        if len(source_names) != len(set(source_names)):
            raise ValueError("configured source name 不可重複")
        aliases: dict[str, list[str]] = defaultdict(list)
        for unit in self.units:
            for alias in (unit.canonical_name, *unit.aliases):
                aliases[alias].append(unit.canonical_name)
        ambiguous = {
            alias: units for alias, units in aliases.items() if len(set(units)) > 1
        }
        if ambiguous:
            details = "；".join(
                f"{alias}={','.join(sorted(set(units)))}"
                for alias, units in sorted(ambiguous.items())
            )
            raise ValueError(f"單位 alias 不可對應多個單位：{details}")
        return self


class OfficialUnitDirectory:
    def __init__(self, payload: OfficialUnitDirectoryPayload) -> None:
        self.source_url = payload.source_url
        self.verified_at = payload.verified_at
        self.units = tuple(unit.resolved() for unit in payload.units)
        self._configs = {unit.canonical_name: unit for unit in payload.units}
        self._by_canonical = {unit.canonical_name: unit for unit in self.units}
        alias_map = {
            alias: unit.canonical_name
            for unit in self.units
            for alias in (unit.canonical_name, *unit.aliases)
        }
        self._aliases = alias_map
        self._matcher = AliasNormalizer(alias_map)

    @property
    def aliases(self) -> Mapping[str, str]:
        return self._aliases

    @property
    def active_units(self) -> tuple[ResolvedOfficialUnit, ...]:
        return tuple(unit for unit in self.units if unit.enabled)

    def get(self, canonical_name: str) -> ResolvedOfficialUnit | None:
        return self._by_canonical.get(canonical_name)

    def mentioned_units(self, text: str | None) -> tuple[ResolvedOfficialUnit, ...]:
        if not text:
            return ()
        canonical_names = {match.canonical for match in self._matcher.matches(text)}
        return tuple(self._by_canonical[name] for name in sorted(canonical_names))

    def scope_for(self, unit: ResolvedOfficialUnit) -> DocumentSearchScope:
        return DocumentSearchScope(
            canonical_unit=unit.canonical_name,
            homepage_url=unit.homepage_url,
            preferred_hosts=unit.preferred_hosts,
            allowed_hosts=unit.allowed_hosts,
            seed_urls=unit.seed_urls,
        )

    def alias_summary(self) -> str:
        grouped: dict[str, list[str]] = defaultdict(list)
        for alias, canonical in self._aliases.items():
            if alias != canonical:
                grouped[canonical].append(alias)
        return "；".join(
            f"{'、'.join(aliases)}＝{canonical}"
            for canonical, aliases in sorted(grouped.items())
        )

    def configured_listing_configs(self) -> Iterable[OfficialUnitConfig]:
        return (
            self._configs[unit.canonical_name]
            for unit in self.units
            if unit.enabled
            and unit.announcement_strategy is AnnouncementStrategy.CONFIGURED_LISTING
        )


def load_official_unit_directory(path: Path) -> OfficialUnitDirectory:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("official unit directory 必須是 mapping")
    return OfficialUnitDirectory(OfficialUnitDirectoryPayload.model_validate(payload))


@lru_cache(maxsize=1)
def load_default_official_unit_directory() -> OfficialUnitDirectory:
    return load_official_unit_directory(DEFAULT_OFFICIAL_UNITS_PATH)
