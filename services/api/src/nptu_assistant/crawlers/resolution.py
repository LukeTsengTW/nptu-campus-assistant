from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from enum import StrEnum

from nptu_assistant.crawlers.aliases import AliasNormalizer
from nptu_assistant.crawlers.config import CrawlerSourceConfig


_ANNOUNCEMENT_TERMS = ("公告", "最新", "消息", "訊息", "通知")
_UNIT_LIKE_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,}?(?:學院|學系|學程|中心|系|處|組|室)"
)
_UNKNOWN_PREFIX = re.compile(
    r"^(?:(?:請問|麻煩|幫我查|幫我|查詢|查|想知道|以及|還有|和|與|及|、)+)"
)


class UnitResolutionStatus(StrEnum):
    NONE = "none"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class UnitResolution:
    status: UnitResolutionStatus
    requested: str
    canonical_unit: str | None = None
    candidates: tuple[str, ...] = ()
    source: CrawlerSourceConfig | None = None


class UnitSourceResolver:
    """Resolve a unit mention to exactly one configured official source."""

    def __init__(
        self,
        sources: Sequence[CrawlerSourceConfig],
        known_aliases: Mapping[str, str],
        source_routes: Mapping[str, str] | None = None,
    ) -> None:
        alias_to_units: dict[str, set[str]] = defaultdict(set)
        unit_to_sources: dict[str, list[CrawlerSourceConfig]] = defaultdict(list)
        source_by_name = {source.name: source for source in sources}
        configured_routes = dict(source_routes or {})
        unknown_routes = sorted(set(configured_routes.values()) - set(source_by_name))
        if unknown_routes:
            raise ValueError(
                "公告來源路由指向未設定的來源：" + "、".join(unknown_routes)
            )

        for alias, canonical in known_aliases.items():
            alias = alias.strip()
            canonical = canonical.strip()
            if alias and canonical:
                alias_to_units[alias].add(canonical)
                alias_to_units[canonical].add(canonical)

        for source in sources:
            canonical = source.unit.strip()
            unit_to_sources[canonical].append(source)
            alias_to_units[canonical].add(canonical)
            for alias in source.aliases:
                alias_to_units[alias].add(canonical)

        self._alias_to_units = {
            alias: frozenset(units) for alias, units in alias_to_units.items()
        }
        self._matcher = AliasNormalizer({alias: alias for alias in alias_to_units})
        self._source_route_targets = {
            alias: source_by_name[source_name]
            for alias, source_name in configured_routes.items()
        }
        self._source_route_matcher = AliasNormalizer(
            {alias: source_name for alias, source_name in configured_routes.items()}
        )
        self._unit_to_sources = {
            unit: tuple(configs) for unit, configs in unit_to_sources.items()
        }

    def _mentioned_units(self, text: str | None) -> set[str]:
        if not text:
            return set()
        units: set[str] = set()
        for match in self._matcher.matches(text):
            units.update(self._alias_to_units[match.alias])
        return units

    def _unknown_unit_mentions(
        self,
        text: str | None,
        *,
        require_announcement_intent: bool,
    ) -> set[str]:
        if not text or (
            require_announcement_intent
            and not any(term in text for term in _ANNOUNCEMENT_TERMS)
        ):
            return set()
        known_spans = [(match.start, match.end) for match in self._matcher.matches(text)]
        unknown: set[str] = set()
        for match in _UNIT_LIKE_PATTERN.finditer(text):
            if any(
                max(match.start(), start) < min(match.end(), end)
                for start, end in known_spans
            ):
                continue
            candidate = _UNKNOWN_PREFIX.sub("", match.group(0)).strip()
            if candidate:
                unknown.add(candidate)
        return unknown

    def _mentioned_source_routes(self, text: str | None) -> tuple[CrawlerSourceConfig, ...]:
        if not text:
            return ()
        matches = self._source_route_matcher.matches(text)
        if not matches or not any(term in text for term in ("獎學金", "獎助學金")):
            return ()
        if "校內" in text:
            internal_matches = tuple(
                self._source_route_targets[match.alias]
                for match in matches
                if match.alias == "校內" or match.alias.startswith("校內")
            )
            if internal_matches:
                return internal_matches
        return tuple(
            self._source_route_targets[match.alias]
            for match in matches
        )

    def resolve(self, unit: str | None, query: str | None = None) -> UnitResolution:
        requested_unit = unit.strip() if unit and unit.strip() else ""
        requested = requested_unit or (query or "").strip()
        units = self._mentioned_units(requested_unit)
        units.update(self._mentioned_units(query))
        unknown_units = self._unknown_unit_mentions(
            requested_unit,
            require_announcement_intent=False,
        )
        unknown_units.update(
            self._unknown_unit_mentions(query, require_announcement_intent=True)
        )

        if units and unknown_units:
            return UnitResolution(
                UnitResolutionStatus.AMBIGUOUS,
                requested,
                candidates=tuple(sorted(units | unknown_units)),
            )

        if len(units) > 1:
            return UnitResolution(
                UnitResolutionStatus.AMBIGUOUS,
                requested,
                candidates=tuple(sorted(units)),
            )

        if unknown_units:
            if len(unknown_units) > 1:
                return UnitResolution(
                    UnitResolutionStatus.AMBIGUOUS,
                    requested,
                    candidates=tuple(sorted(unknown_units)),
                )
            return UnitResolution(
                UnitResolutionStatus.UNKNOWN,
                next(iter(unknown_units)),
            )

        route_text = " ".join(
            part.strip() for part in (unit, query) if part and part.strip()
        )
        route_sources = self._mentioned_source_routes(route_text)
        distinct_route_sources = {source.name: source for source in route_sources}
        if len(distinct_route_sources) > 1:
            return UnitResolution(
                UnitResolutionStatus.AMBIGUOUS,
                requested,
                candidates=tuple(sorted(distinct_route_sources)),
            )
        if distinct_route_sources:
            source = next(iter(distinct_route_sources.values()))
            if units and source.unit not in units:
                return UnitResolution(
                    UnitResolutionStatus.AMBIGUOUS,
                    requested,
                    candidates=tuple(sorted(units | {source.unit})),
                )
            if not source.enabled:
                return UnitResolution(
                    UnitResolutionStatus.UNSUPPORTED,
                    requested,
                    canonical_unit=source.unit,
                )
            return UnitResolution(
                UnitResolutionStatus.RESOLVED,
                requested,
                canonical_unit=source.unit,
                source=source,
            )

        if not units:
            if requested_unit:
                return UnitResolution(UnitResolutionStatus.UNKNOWN, requested)
            return UnitResolution(UnitResolutionStatus.NONE, requested)

        canonical = next(iter(units))
        enabled_sources = tuple(
            source
            for source in self._unit_to_sources.get(canonical, ())
            if source.enabled
        )
        if not enabled_sources:
            return UnitResolution(
                UnitResolutionStatus.UNSUPPORTED,
                requested,
                canonical_unit=canonical,
            )
        if len(enabled_sources) > 1:
            return UnitResolution(
                UnitResolutionStatus.AMBIGUOUS,
                requested,
                candidates=tuple(sorted({source.unit for source in enabled_sources})),
            )
        return UnitResolution(
            UnitResolutionStatus.RESOLVED,
            requested,
            canonical_unit=canonical,
            source=enabled_sources[0],
        )
