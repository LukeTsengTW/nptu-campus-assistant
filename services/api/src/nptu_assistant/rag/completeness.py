"""Pure DB-first retrieval completeness policy.

The policy deliberately only evaluates supplied facts.  It performs no I/O and
does not consult process-global time so the caller can use the same decision in
request handling, shadow telemetry, and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CompletenessAction(StrEnum):
    USE_DB = "use_db"
    USE_DB_AND_SCHEDULE_REFRESH = "use_db_and_schedule_refresh"
    USE_BOUNDED_LIVE_FALLBACK = "use_bounded_live_fallback"
    USE_DB_WITH_INCOMPLETE_WARNING = "use_db_with_incomplete_warning"


class CompletenessMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


class QueryIntent(StrEnum):
    TOPIC = "topic"
    SCOPED_TOPIC = "scoped_topic"
    LATEST = "latest"
    ANNOUNCEMENT = "announcement"
    KEYWORD_ANNOUNCEMENT = "keyword_announcement"


@dataclass(frozen=True, slots=True)
class CompletenessConfig:
    min_strong_evidence: int = 2
    exact_scope_min_score: float = 0.82
    minimum_score_margin: float = 0.04
    minimum_remaining_deadline_seconds: float = 2.0
    minimum_source_coverage_ratio: float = 0.60
    document_soft_stale_minutes: int = 360
    document_hard_stale_minutes: int = 1_440
    announcement_soft_stale_minutes: int = 90
    announcement_hard_stale_minutes: int = 360
    live_fallback_enabled: bool = True
    live_fallback_max_details: int = 4
    rollout_mode: CompletenessMode = CompletenessMode.ENFORCE

    def __post_init__(self) -> None:
        if self.min_strong_evidence < 1:
            raise ValueError("min_strong_evidence 必須至少為 1")
        if not 0.0 <= self.exact_scope_min_score <= 1.0:
            raise ValueError("exact_scope_min_score 必須介於 0 與 1")
        if self.minimum_score_margin < 0.0:
            raise ValueError("minimum_score_margin 不可為負數")
        if self.minimum_remaining_deadline_seconds < 0.0:
            raise ValueError("minimum_remaining_deadline_seconds 不可為負數")
        if self.live_fallback_max_details < 1:
            raise ValueError("live_fallback_max_details 必須至少為 1")
        if not 0.0 <= self.minimum_source_coverage_ratio <= 1.0:
            raise ValueError("minimum_source_coverage_ratio 必須介於 0 與 1")
        if not 0 < self.document_soft_stale_minutes <= self.document_hard_stale_minutes:
            raise ValueError("文件 freshness window 不正確")
        if (
            not 0
            < self.announcement_soft_stale_minutes
            <= self.announcement_hard_stale_minutes
        ):
            raise ValueError("公告 freshness window 不正確")


@dataclass(frozen=True, slots=True)
class CompletenessFacts:
    """Set-based retrieval facts collected outside the policy.

    Counts only describe current/persisted evidence.  The policy never treats a
    missing fact as affirmative proof of completeness.
    """

    evidence_count: int = 0
    unique_url_count: int = 0
    strong_evidence_count: int = 0
    top_score: float = 0.0
    score_margin: float = 0.0
    current_document_count: int = 0
    superseded_document_count: int = 0
    exact_scope_match_count: int = 0
    homepage_only_count: int = 0
    listing_only_count: int = 0
    fresh_count: int = 0
    soft_stale_count: int = 0
    hard_stale_count: int = 0
    content_hash_in_sync_count: int = 0
    pending_ingestion_count: int = 0
    failed_ingestion_count: int = 0
    incomplete_announcement_count: int = 0
    active_refresh_count: int = 0
    source_coverage_ratio: float = 0.0
    facts_query_succeeded: bool = True
    canonical_urls: tuple[str, ...] = ()
    source_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletenessDecision:
    action: CompletenessAction
    reason_codes: tuple[str, ...]
    confidence: float
    schedule_targets: tuple[str, ...] = ()
    schedule_source_names: tuple[str, ...] = ()


class DbFirstCompletenessPolicy:
    """Conservative, deterministic policy for DB-first retrieval decisions."""

    def __init__(self, config: CompletenessConfig | None = None) -> None:
        self._config = config or CompletenessConfig()

    @property
    def config(self) -> CompletenessConfig:
        return self._config

    def decide(
        self,
        *,
        facts: CompletenessFacts,
        intent: QueryIntent,
        remaining_deadline_seconds: float,
    ) -> CompletenessDecision:
        announcement_intent = intent in {
            QueryIntent.ANNOUNCEMENT,
            QueryIntent.KEYWORD_ANNOUNCEMENT,
            QueryIntent.LATEST,
        }
        usable_announcement_evidence = bool(
            announcement_intent
            and facts.evidence_count
            and facts.current_document_count
        )

        if not facts.facts_query_succeeded:
            return self._insufficient(
                facts,
                "facts_query_failed",
                remaining_deadline_seconds,
            )

        if facts.active_refresh_count:
            return self._decision(
                CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING,
                facts,
                "active_refresh_in_progress",
            )

        if facts.homepage_only_count and intent in {
            QueryIntent.TOPIC,
            QueryIntent.SCOPED_TOPIC,
        }:
            return self._insufficient(
                facts,
                "homepage_only_for_topic_query",
                remaining_deadline_seconds,
            )

        if facts.listing_only_count and intent in {
            QueryIntent.TOPIC,
            QueryIntent.SCOPED_TOPIC,
        }:
            return self._insufficient(
                facts,
                "missing_listing_coverage",
                remaining_deadline_seconds,
            )

        if facts.current_document_count == 0 and intent in {
            QueryIntent.TOPIC,
            QueryIntent.SCOPED_TOPIC,
        }:
            return self._insufficient(
                facts,
                "site_map_only_without_document",
                remaining_deadline_seconds,
            )

        if facts.incomplete_announcement_count and intent in {
            QueryIntent.ANNOUNCEMENT,
            QueryIntent.KEYWORD_ANNOUNCEMENT,
        }:
            return self._decision(
                CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING,
                facts,
                "terminal_incomplete",
            )

        if facts.current_document_count == 0 and announcement_intent:
            return self._insufficient(
                facts,
                "missing_listing_coverage",
                remaining_deadline_seconds,
            )

        if intent is QueryIntent.SCOPED_TOPIC and facts.exact_scope_match_count == 0:
            return self._insufficient(
                facts,
                "missing_unit_coverage",
                remaining_deadline_seconds,
            )

        # Freshness and ingestion state are stronger signals than semantic
        # scores for announcement lists. Newest and configured listing queries
        # are ordered/filter operations, so a score of zero is not evidence that
        # the snapshot is unusable.
        if facts.hard_stale_count:
            if intent is QueryIntent.LATEST:
                return self._insufficient(
                    facts,
                    "recent_query_requires_freshness",
                    remaining_deadline_seconds,
                )
            return self._decision(
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                facts,
                "hard_stale",
            )

        requires_page_hash_fencing = intent in {
            QueryIntent.TOPIC,
            QueryIntent.SCOPED_TOPIC,
        }
        if facts.pending_ingestion_count or (
            requires_page_hash_fencing and facts.content_hash_in_sync_count < 1
        ):
            return self._decision(
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                facts,
                "relevant_ingestion_pending",
            )

        if facts.failed_ingestion_count:
            return self._decision(
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                facts,
                "relevant_ingestion_failed",
            )

        if facts.soft_stale_count:
            return self._decision(
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                facts,
                "stale_but_usable",
            )

        if (
            intent in {QueryIntent.LATEST, QueryIntent.ANNOUNCEMENT}
            and usable_announcement_evidence
            and facts.fresh_count
        ):
            return self._decision(
                CompletenessAction.USE_DB,
                facts,
                "fresh_announcement_snapshot",
            )

        if (
            announcement_intent
            and facts.source_coverage_ratio < self._config.minimum_source_coverage_ratio
        ):
            return self._decision(
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                facts,
                "insufficient_source_coverage",
            )

        if facts.strong_evidence_count == 0:
            if (
                intent is QueryIntent.KEYWORD_ANNOUNCEMENT
                and usable_announcement_evidence
                and facts.fresh_count
            ):
                return self._decision(
                    CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                    facts,
                    "weak_but_usable_announcement_evidence",
                )
            return self._insufficient(
                facts,
                "weak_retrieval_score",
                remaining_deadline_seconds,
            )

        exact_scoped = (
            intent in {QueryIntent.SCOPED_TOPIC, QueryIntent.ANNOUNCEMENT}
            and facts.exact_scope_match_count > 0
            and facts.top_score >= self._config.exact_scope_min_score
            and facts.fresh_count > 0
            and (
                facts.content_hash_in_sync_count > 0
                or (
                    intent is QueryIntent.ANNOUNCEMENT
                    and facts.current_document_count > 0
                )
            )
        )
        enough_strong = facts.strong_evidence_count >= self._config.min_strong_evidence
        if exact_scoped:
            return self._decision(
                CompletenessAction.USE_DB,
                facts,
                "exact_scoped_match",
            )
        if (
            enough_strong
            and facts.fresh_count
            and (
                facts.score_margin >= self._config.minimum_score_margin
                or facts.strong_evidence_count > 1
            )
        ):
            return self._decision(
                CompletenessAction.USE_DB,
                facts,
                "fresh_strong_evidence",
            )
        if facts.score_margin < self._config.minimum_score_margin:
            if (
                intent is QueryIntent.KEYWORD_ANNOUNCEMENT
                and usable_announcement_evidence
                and facts.fresh_count
            ):
                return self._decision(
                    CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                    facts,
                    "ambiguous_but_usable_announcement_evidence",
                )
            return self._insufficient(
                facts,
                "insufficient_score_margin",
                remaining_deadline_seconds,
            )
        if (
            intent is QueryIntent.KEYWORD_ANNOUNCEMENT
            and usable_announcement_evidence
            and facts.fresh_count
        ):
            return self._decision(
                CompletenessAction.USE_DB_AND_SCHEDULE_REFRESH,
                facts,
                "sparse_but_usable_announcement_evidence",
            )
        return self._insufficient(
            facts,
            "insufficient_result_count",
            remaining_deadline_seconds,
        )

    def _insufficient(
        self,
        facts: CompletenessFacts,
        reason: str,
        remaining_deadline_seconds: float,
    ) -> CompletenessDecision:
        if not self._config.live_fallback_enabled:
            return self._decision(
                CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING,
                facts,
                reason,
                "live_fallback_disabled",
            )
        if remaining_deadline_seconds < self._config.minimum_remaining_deadline_seconds:
            return self._decision(
                CompletenessAction.USE_DB_WITH_INCOMPLETE_WARNING,
                facts,
                reason,
                "deadline_insufficient",
            )
        return self._decision(
            CompletenessAction.USE_BOUNDED_LIVE_FALLBACK,
            facts,
            reason,
        )

    @staticmethod
    def _decision(
        action: CompletenessAction,
        facts: CompletenessFacts,
        *reason_codes: str,
    ) -> CompletenessDecision:
        confidence = max(0.0, min(1.0, facts.top_score))
        targets = tuple(dict.fromkeys(facts.canonical_urls))
        return CompletenessDecision(
            action=action,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            confidence=confidence,
            schedule_targets=targets,
            schedule_source_names=tuple(dict.fromkeys(facts.source_names)),
        )