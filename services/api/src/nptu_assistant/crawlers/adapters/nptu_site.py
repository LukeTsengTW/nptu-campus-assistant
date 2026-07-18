from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)
from nptu_assistant.crawlers.parsing import parse_published_at
from nptu_assistant.ingestion.cleaning import extract_clean_html, normalize_text


_DATE_PATTERN = re.compile(r"(?:\d{3,4})[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?")
_RESOURCE_SUFFIXES = frozenset(
    {
        ".7z",
        ".avi",
        ".css",
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".gz",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".mov",
        ".mp3",
        ".mp4",
        ".pdf",
        ".png",
        ".ppt",
        ".pptx",
        ".rar",
        ".svg",
        ".tar",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
        ".xls",
        ".xlsx",
        ".xml",
        ".zip",
    }
)
_MAX_PAGE_TEXT = 20_000


@dataclass(frozen=True, slots=True)
class NptuSitePage:
    title: str
    canonical_url: str
    body: str
    published_at: date | None
    links: tuple[str, ...]
    link_texts: tuple[tuple[str, str], ...] = ()
    headings: tuple[str, ...] = ()
    score: float = 0.0


class NptuSitePageAdapter:
    def parse_page(
        self,
        content: str,
        page_url: str,
        *,
        allowed_hosts: list[str] | tuple[str, ...],
    ) -> NptuSitePage:
        canonical_url = canonicalize_nptu_url(page_url)
        if not is_allowed_source_url(canonical_url, allowed_hosts):
            raise ValueError("網站頁面不在來源 host allowlist")

        soup = BeautifulSoup(content, "html.parser")
        title = self._title(soup)
        body = extract_clean_html(content)
        if title and not body.startswith(title):
            body = f"{title}\n{body}" if body else title
        body = normalize_text(body)[:_MAX_PAGE_TEXT]
        headings = tuple(
            dict.fromkeys(
                heading
                for node in soup.select("h1, h2, h3, h4, h5, h6")
                if isinstance(node, Tag)
                and (heading := normalize_text(node.get_text(" ", strip=True)))
            )
        )
        link_texts = tuple(
            self._link_details(soup, canonical_url, allowed_hosts=allowed_hosts)
        )
        links = tuple(url for url, _label in link_texts)
        return NptuSitePage(
            title=title or canonical_url,
            canonical_url=canonical_url,
            body=body,
            published_at=self._published_at(soup, body),
            links=links,
            link_texts=link_texts,
            headings=headings,
        )

    @staticmethod
    def _title(soup: BeautifulSoup) -> str:
        for selector in (
            'meta[property="og:title"]',
            'meta[name="title"]',
            "h1",
            "title",
        ):
            node = soup.select_one(selector)
            if not isinstance(node, Tag):
                continue
            value = (
                node.get("content")
                if node.name == "meta"
                else node.get_text(" ", strip=True)
            )
            title = normalize_text(str(value or ""))
            if title:
                return title
        return ""

    @staticmethod
    def _published_at(soup: BeautifulSoup, body: str) -> date | None:
        candidates: list[str] = []
        for node in soup.select(
            'meta[property="article:published_time"], '
            'meta[property="og:updated_time"], '
            'meta[name="date"], '
            'meta[name="publishdate"], '
            'meta[name="pubdate"]'
        ):
            if isinstance(node, Tag) and node.get("content"):
                candidates.append(str(node.get("content")))
        for node in soup.select(
            "time[datetime], [data-date], .date, .mdate, .pdate, "
            '[class*="date"], [id*="date"]'
        ):
            if not isinstance(node, Tag):
                continue
            candidates.append(
                str(
                    node.get("datetime")
                    or node.get("data-date")
                    or node.get_text(" ", strip=True)
                )
            )
        candidates.append(body[:5_000])
        for value in candidates:
            match = _DATE_PATTERN.search(value)
            if not match:
                continue
            try:
                return parse_published_at(match.group(0))
            except ValueError:
                continue
        return None

    @classmethod
    def _links(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        *,
        allowed_hosts: list[str] | tuple[str, ...],
    ) -> list[str]:
        return [
            url
            for url, _label in cls._link_details(
                soup,
                page_url,
                allowed_hosts=allowed_hosts,
            )
        ]

    @classmethod
    def _link_details(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        *,
        allowed_hosts: list[str] | tuple[str, ...],
    ) -> list[tuple[str, str]]:
        links: list[str] = []
        labels: dict[str, list[str]] = {}
        for node in soup.find_all("a", href=True):
            if not isinstance(node, Tag):
                continue
            raw_href = str(node.get("href") or "").strip()
            if not raw_href or raw_href.startswith(
                ("#", "mailto:", "tel:", "javascript:")
            ):
                continue
            target = urljoin(page_url, raw_href)
            if not is_allowed_nptu_url(target):
                continue
            if not is_allowed_source_url(
                target, allowed_hosts
            ) or not cls.is_crawlable_url(target):
                continue
            try:
                canonical_url = canonicalize_nptu_url(target)
            except ValueError:
                continue
            if canonical_url not in labels:
                links.append(canonical_url)
                labels[canonical_url] = []
            label = normalize_text(
                " ".join(
                    str(value).strip()
                    for value in (
                        node.get("aria-label"),
                        node.get("title"),
                        node.get_text(" ", strip=True),
                    )
                    if value
                )
            )
            if label and label not in labels[canonical_url]:
                labels[canonical_url].append(label)
        return [(url, " ".join(labels[url])) for url in links]

    @staticmethod
    def is_crawlable_url(url: str) -> bool:
        path = urlsplit(url).path.lower()
        return not any(path.endswith(suffix) for suffix in _RESOURCE_SUFFIXES)
