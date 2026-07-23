from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from nptu_assistant.core.security import (
    canonicalize_nptu_url,
    is_allowed_nptu_url,
    is_allowed_source_url,
)
from nptu_assistant.crawlers.parsing import parse_published_at
from nptu_assistant.crawlers.crawl_policy import is_crawlable_url
from nptu_assistant.ingestion.cleaning import extract_clean_html, normalize_text


_DATE_PATTERN = re.compile(r"(?:\d{3,4})[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?")
_MAX_PAGE_TEXT = 20_000


class UnitAnnouncementPageRole(StrEnum):
    OTHER = "other"
    LISTING = "listing"
    DETAIL = "detail"


@dataclass(frozen=True, slots=True)
class NptuListingItem:
    title: str
    canonical_url: str
    published_at: date | None
    summary: str
    anchor_text: str
    order: int


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
    role: UnitAnnouncementPageRole = UnitAnnouncementPageRole.OTHER
    announcement_items: tuple[NptuListingItem, ...] = ()


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
        announcement_items = tuple(
            self._listing_items(
                soup,
                canonical_url,
                allowed_hosts=allowed_hosts,
            )
        )
        published_at = self._published_at(soup, body)
        return NptuSitePage(
            title=title or canonical_url,
            canonical_url=canonical_url,
            body=body,
            published_at=published_at,
            links=links,
            link_texts=link_texts,
            headings=headings,
            role=self._page_role(
                soup,
                canonical_url,
                title=title,
                body=body,
                published_at=published_at,
                announcement_items=announcement_items,
            ),
            announcement_items=announcement_items,
        )

    @classmethod
    def _listing_items(
        cls,
        soup: BeautifulSoup,
        page_url: str,
        *,
        allowed_hosts: list[str] | tuple[str, ...],
    ) -> list[NptuListingItem]:
        rows = soup.select(
            ".row.listBS, table.listTB tbody tr, "
            ".module .mtitle, .module-detail .mtitle, .mb .mtitle"
        )
        items: list[NptuListingItem] = []
        seen: set[str] = set()
        for order, matched_node in enumerate(rows):
            if not isinstance(matched_node, Tag):
                continue
            item_node = matched_node
            if "mtitle" in (matched_node.get("class") or []):
                for parent in matched_node.parents:
                    if not isinstance(parent, Tag):
                        continue
                    parent_classes = set(parent.get("class") or [])
                    if parent.name == "tr" or parent_classes.intersection(
                        {"d-txt", "row", "listBS"}
                    ):
                        item_node = parent
                        break
            anchor = matched_node.select_one(".mtitle > a[href], a[href]")
            if not isinstance(anchor, Tag):
                continue
            raw_href = str(anchor.get("href") or "").strip()
            if not raw_href or raw_href.startswith(
                ("#", "mailto:", "tel:", "javascript:")
            ):
                continue
            target = urljoin(page_url, raw_href)
            if (
                not is_allowed_nptu_url(target)
                or not is_allowed_source_url(target, allowed_hosts)
                or not cls.is_crawlable_url(target)
            ):
                continue
            try:
                canonical_url = canonicalize_nptu_url(target)
            except ValueError:
                continue
            title = normalize_text(anchor.get_text(" ", strip=True))
            if not title or canonical_url in seen:
                continue
            date_node = item_node.select_one(
                'i.mdate, td[data-th="日期"], .mdate, .date, time, [data-date]'
            )
            published_at = None
            if isinstance(date_node, Tag):
                date_text = str(
                    date_node.get("datetime")
                    or date_node.get("data-date")
                    or date_node.get_text(" ", strip=True)
                )
                match = _DATE_PATTERN.search(date_text)
                if match:
                    try:
                        published_at = parse_published_at(match.group(0))
                    except ValueError:
                        pass
            detail_path = urlsplit(canonical_url).path.casefold()
            if "/p/406-" not in detail_path and published_at is None:
                continue
            summary_node = item_node.select_one(
                ".mdetail, .summary, .mcont, .d-txt, td[data-th='內容']"
            )
            summary = (
                normalize_text(summary_node.get_text(" ", strip=True))
                if isinstance(summary_node, Tag)
                else ""
            )
            seen.add(canonical_url)
            items.append(
                NptuListingItem(
                    title=title,
                    canonical_url=canonical_url,
                    published_at=published_at,
                    summary=summary,
                    anchor_text=normalize_text(
                        " ".join(
                            str(value)
                            for value in (anchor.get("title"), title)
                            if value
                        )
                    ),
                    order=order,
                )
            )
        return items

    @staticmethod
    def _page_role(
        soup: BeautifulSoup,
        canonical_url: str,
        *,
        title: str,
        body: str,
        published_at: date | None,
        announcement_items: tuple[NptuListingItem, ...],
    ) -> UnitAnnouncementPageRole:
        path = urlsplit(canonical_url).path.casefold()
        has_listing_dom = bool(
            soup.select_one(".row.listBS, table.listTB tbody tr, .module .mtitle")
        )
        if announcement_items and (
            has_listing_dom
            or len(announcement_items) >= 2
            or "/p/403-" in path
            or "/p/404-" in path
        ):
            return UnitAnnouncementPageRole.LISTING
        has_detail_dom = bool(
            soup.select_one(
                "article, .mpgdetail, .module-detail, .news-detail, "
                ".article-detail, [class*='detail']"
            )
        )
        if (
            not announcement_items
            and title
            and len(body) >= 40
            and (has_detail_dom or (published_at is not None and "/p/406-" in path))
        ):
            return UnitAnnouncementPageRole.DETAIL
        return UnitAnnouncementPageRole.OTHER

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
            target_parts = urlsplit(target)
            if (
                target_parts.fragment
                and not target_parts.path
                and not target_parts.query
            ):
                continue
            try:
                canonical_url = canonicalize_nptu_url(target)
            except ValueError:
                continue
            if not is_allowed_source_url(
                canonical_url, allowed_hosts
            ) or not cls.is_crawlable_url(canonical_url):
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
        return is_crawlable_url(url)
