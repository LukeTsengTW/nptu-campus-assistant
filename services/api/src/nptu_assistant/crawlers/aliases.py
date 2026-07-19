from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re


_UNIT_LIKE_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9]{2,}?(?:學院|學系|研究所|學程|中心|系|所|處|組|室)"
)
_UNIT_REQUEST_PREFIX = re.compile(
    r"^(?:(?:國立屏東大學|屏東大學|屏大|請問|麻煩|幫我查|幫我|查詢|查|"
    r"想知道|比較|以及|還有|和|與|及|、)+)"
)


@dataclass(frozen=True, slots=True)
class AliasMatch:
    alias: str
    canonical: str
    start: int
    end: int


class AliasNormalizer:
    """Deterministically match and normalize known aliases in text."""

    def __init__(
        self,
        aliases: Mapping[str, str],
        *,
        unit_aware: bool = False,
    ) -> None:
        self._aliases = {
            alias.strip(): canonical.strip()
            for alias, canonical in aliases.items()
            if alias.strip() and canonical.strip()
        }
        ordered = sorted(self._aliases, key=lambda alias: (-len(alias), alias))
        self._pattern = (
            re.compile("|".join(re.escape(alias) for alias in ordered))
            if ordered
            else None
        )
        self._unit_aware = unit_aware

    def _unit_like_spans(self, text: str) -> tuple[tuple[int, int, str], ...]:
        if not self._unit_aware:
            return ()
        spans: list[tuple[int, int, str]] = []
        for match in _UNIT_LIKE_PATTERN.finditer(text):
            raw = match.group(0)
            candidate = _UNIT_REQUEST_PREFIX.sub("", raw)
            if not candidate:
                continue
            start = match.end() - len(candidate)
            spans.append((start, match.end(), candidate))
        return tuple(spans)

    def matches(self, text: str) -> tuple[AliasMatch, ...]:
        if self._pattern is None:
            return ()
        matches = tuple(
            AliasMatch(
                alias=match.group(0),
                canonical=self._aliases[match.group(0)],
                start=match.start(),
                end=match.end(),
            )
            for match in self._pattern.finditer(text)
        )
        unit_spans = self._unit_like_spans(text)
        if not unit_spans:
            return matches
        return tuple(
            match
            for match in matches
            if not any(
                start <= match.start
                and match.end <= end
                and candidate not in self._aliases
                for start, end, candidate in unit_spans
            )
        )

    def normalize(self, text: str) -> str:
        normalized = text.strip()
        if self._pattern is None:
            return normalized
        if self._unit_aware:
            for match in reversed(self.matches(normalized)):
                normalized = (
                    normalized[: match.start]
                    + match.canonical
                    + normalized[match.end :]
                )
            return normalized
        return self._pattern.sub(
            lambda match: self._aliases[match.group(0)],
            normalized,
        )
