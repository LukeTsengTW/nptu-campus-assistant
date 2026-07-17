from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from nptu_assistant.core.security import is_allowed_nptu_url
from nptu_assistant.crawlers.parsing import parse_published_at
from nptu_assistant.ingestion.cleaning import extract_clean_html


_DATE_PATTERN = re.compile(r"(?:20\d{2}|1\d{2})[-/.年]\d{1,2}[-/.月]\d{1,2}")


@dataclass(frozen=True, slots=True)
class SearchForm:
    method: Literal["get", "post"]
    action_url: str
    hidden_fields: dict[str, str]
    search_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BootstrapForm:
    method: Literal["get", "post"]
    hidden_fields: dict[str, str]


@dataclass(frozen=True, slots=True)
class AnnouncementSearchResult:
    title: str
    canonical_url: str
    unit: str
    category: str | None
    published_at: date | None
    body: str
    source_name: str | None = None
    source_url: str | None = None


class NptuAssociationSearchAdapter:
    @staticmethod
    def _hidden_fields(form: Tag) -> dict[str, str]:
        return {
            str(field.get("name")): str(field.get("value", ""))
            for field in form.find_all("input", attrs={"type": "hidden"})
            if isinstance(field, Tag) and field.get("name")
        }

    def parse_bootstrap_form(self, content: str, page_url: str) -> BootstrapForm:
        if not is_allowed_nptu_url(page_url):
            raise ValueError("bootstrap URL 不在 NPTU allowlist")
        soup = BeautifulSoup(content, "html.parser")
        for form in soup.find_all("form"):
            if not isinstance(form, Tag) or form.find(attrs={"name": "SchKey"}) is None:
                continue
            method = str(form.get("method", "get")).lower()
            if method not in {"get", "post"}:
                raise ValueError("搜尋表單僅允許 GET 或 POST")
            return BootstrapForm(method, self._hidden_fields(form))
        raise ValueError("找不到含 SchKey 的 bootstrap 搜尋表單")

    def parse_form(self, content: str, page_url: str) -> SearchForm:
        soup = BeautifulSoup(content, "html.parser")
        for form in soup.find_all("form"):
            if not isinstance(form, Tag):
                continue
            keyword = form.find(attrs={"name": "SchKey"})
            search_type = form.find("select", attrs={"name": "SchType"})
            if keyword is None or not isinstance(search_type, Tag):
                continue
            method = str(form.get("method", "get")).lower()
            if method not in {"get", "post"}:
                raise ValueError("搜尋表單僅允許 GET 或 POST")
            action_url = urljoin(page_url, str(form.get("action") or page_url))
            if not is_allowed_nptu_url(action_url):
                raise ValueError("搜尋表單 action 不在 NPTU allowlist")
            hidden_fields = self._hidden_fields(form)
            search_types = tuple(
                str(option.get("value"))
                for option in search_type.find_all("option")
                if isinstance(option, Tag) and option.get("value")
            )
            return SearchForm(method, action_url, hidden_fields, search_types)
        raise ValueError("找不到含 SchKey 與 SchType 的搜尋表單")

    def parse_results(self, content: str, page_url: str) -> list[AnnouncementSearchResult]:
        soup = BeautifulSoup(content, "html.parser")
        root = soup.select_one(
            "#assopartlist, #assocomlist, [data-search-results], .assosearch, .search-results, #assosearch"
        )
        if root is None:
            raise ValueError("找不到官方搜尋結果區塊")

        results: list[AnnouncementSearchResult] = []
        containers = root.select(".d-item") or root.find_all(["tr", "li", "article"])
        for container in containers:
            if not isinstance(container, Tag):
                continue
            anchor = container.select_one(".mtitle > a[href]") or container.find("a", href=True)
            if not isinstance(anchor, Tag):
                continue
            title = anchor.get_text(" ", strip=True)
            canonical_url = urljoin(page_url, str(anchor.get("href")))
            if not title or not is_allowed_nptu_url(canonical_url):
                continue
            text = container.get_text(" ", strip=True)
            date_node = container.select_one(".date, .mdate, [data-date]")
            date_text = date_node.get_text(" ", strip=True) if date_node else text
            date_match = _DATE_PATTERN.search(date_text)
            published_at = None
            if date_match:
                try:
                    published_at = parse_published_at(date_match.group(0))
                except ValueError:
                    published_at = None
            unit_node = container.select_one(".unit, [data-unit], .subsitename")
            category_node = container.select_one(".category, [data-category]")
            unit = unit_node.get_text(" ", strip=True) if unit_node else "國立屏東大學"
            category = category_node.get_text(" ", strip=True) if category_node else None
            results.append(
                AnnouncementSearchResult(
                    title=title,
                    canonical_url=canonical_url,
                    unit=unit,
                    category=category,
                    published_at=published_at,
                    body=text,
                )
            )
        return results

    def parse_detail(self, content: str) -> str:
        return extract_clean_html(content)
