from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re


@dataclass(frozen=True, slots=True)
class AliasMatch:
    alias: str
    canonical: str
    start: int
    end: int


class AliasNormalizer:
    """Deterministically match and normalize known aliases in text."""

    def __init__(self, aliases: Mapping[str, str]) -> None:
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

    def matches(self, text: str) -> tuple[AliasMatch, ...]:
        if self._pattern is None:
            return ()
        return tuple(
            AliasMatch(
                alias=match.group(0),
                canonical=self._aliases[match.group(0)],
                start=match.start(),
                end=match.end(),
            )
            for match in self._pattern.finditer(text)
        )

    def normalize(self, text: str) -> str:
        normalized = text.strip()
        if self._pattern is None:
            return normalized
        return self._pattern.sub(
            lambda match: self._aliases[match.group(0)],
            normalized,
        )
