from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.adapters.nptu_search import (
    NptuAssociationSearchAdapter,
    SearchForm,
)
from nptu_assistant.crawlers.config import KeywordSearchConfig, SiteSearchConfig
from nptu_assistant.crawlers.site_models import DiscoveredPage, SearchPlan


class DiscoveryHttpClient(Protocol):
    def get(self, url: str) -> str: ...

    def submit_form(self, method: str, url: str, fields: Mapping[str, str]) -> str: ...


class SiteDiscovery(Protocol):
    def discover(
        self, plan: SearchPlan, *, max_items: int
    ) -> tuple[DiscoveredPage, ...]: ...


class NptuOfficialSearchDiscovery:
    """透過 NPTU 官方搜尋表單取得一般頁面候選 URL，不直接把結果視為公告。"""

    def __init__(
        self,
        search_config: KeywordSearchConfig,
        site_config: SiteSearchConfig,
        http_client: DiscoveryHttpClient,
        adapter: NptuAssociationSearchAdapter | None = None,
    ) -> None:
        self._search_config = search_config
        self._site_config = site_config
        self._http = http_client
        self._adapter = adapter or NptuAssociationSearchAdapter()

    def discover(
        self, plan: SearchPlan, *, max_items: int
    ) -> tuple[DiscoveredPage, ...]:
        limit = min(max_items, self._site_config.max_candidate_urls)
        form: SearchForm | None = self._load_form()
        discovered: dict[str, DiscoveredPage] = {}
        for search_query in plan.search_queries:
            for search_type in self._search_config.search_types:
                last_error: Exception | None = None
                for _attempt in range(2):
                    try:
                        if form is None:
                            form = self._load_form()
                        fields = dict(form.hidden_fields)
                        fields.update({"SchKey": search_query, "SchType": search_type})
                        content = self._http.submit_form(
                            form.method, form.action_url, fields
                        )
                        results = self._adapter.parse_results(content, form.action_url)
                        form = self._adapter.parse_form(
                            content, self._search_config.url
                        )
                        for result in results:
                            try:
                                canonical_url = canonicalize_nptu_url(
                                    result.canonical_url
                                )
                            except ValueError:
                                continue
                            if not is_allowed_source_url(
                                canonical_url,
                                self._site_config.allowed_hosts,
                            ):
                                continue
                            rank = len(discovered)
                            discovered.setdefault(
                                canonical_url,
                                DiscoveredPage(
                                    canonical_url,
                                    result.title,
                                    max(0.2, 1.0 - rank / max(1, limit)),
                                ),
                            )
                            if len(discovered) >= limit:
                                return tuple(discovered.values())
                        break
                    except Exception as exc:
                        last_error = exc
                        form = None
                if last_error is not None and form is None:
                    continue
        return tuple(discovered.values())

    def _load_form(self) -> SearchForm:
        self._http.get(self._search_config.session_url)
        content = self._http.submit_form(
            self._search_config.bootstrap_method,
            self._search_config.bootstrap_url,
            {},
        )
        bootstrap = self._adapter.parse_bootstrap_form(
            content,
            self._search_config.bootstrap_url,
        )
        return SearchForm(
            bootstrap.method,
            self._search_config.url,
            bootstrap.hidden_fields,
            tuple(self._search_config.search_types),
        )
