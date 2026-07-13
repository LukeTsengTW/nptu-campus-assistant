from __future__ import annotations

import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from nptu_assistant.core.security import canonicalize_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.config import CrawlerSourceConfig
from nptu_assistant.crawlers.models import AnnouncementCandidate
from nptu_assistant.crawlers.parsing import parse_published_at
from nptu_assistant.ingestion.cleaning import extract_clean_html


logger = logging.getLogger(__name__)


class NptuHtmlListAdapter:
    def __init__(self, config: CrawlerSourceConfig) -> None:
        if config.adapter != "nptu_html_list" or config.selectors is None:
            raise ValueError("HTML adapter 需要 nptu_html_list selectors 設定")
        self._config = config

    def parse_listing(self, content: str) -> list[AnnouncementCandidate]:
        selectors = self._config.selectors
        soup = BeautifulSoup(content, "html.parser")
        listing_roots = soup.select(selectors.listing)
        if not listing_roots:
            raise ValueError("找不到設定的公告列表區塊")
        rows = [row for root in listing_roots for row in root.select(selectors.item)]
        if not rows:
            raise ValueError("公告列表中找不到公告項目")

        candidates: list[AnnouncementCandidate] = []
        seen_urls: set[str] = set()
        for index, row in enumerate(rows):
            try:
                candidate = self._parse_item(row, index=index)
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "html_announcement_item_skipped",
                    extra={
                        "source_name": self._config.name,
                        "item_index": index,
                        "reason": str(exc),
                    },
                )
                continue
            if candidate.canonical_url in seen_urls:
                continue
            seen_urls.add(candidate.canonical_url)
            candidates.append(candidate)

        if not candidates:
            raise ValueError("公告列表沒有可使用的有效公告")
        candidates.sort(key=lambda item: item.published_at, reverse=True)
        return candidates

    def _parse_item(self, row: Tag, *, index: int) -> AnnouncementCandidate:
        del index
        selectors = self._config.selectors
        if selectors is None:
            raise ValueError("缺少 HTML selectors")
        date_node = row.select_one(selectors.date)
        link_node = row.select_one(selectors.title_link)
        if not isinstance(date_node, Tag):
            raise ValueError("公告項目缺少日期")
        if not isinstance(link_node, Tag):
            raise ValueError("公告項目缺少標題連結")

        title = " ".join(link_node.get_text(" ", strip=True).split())
        raw_href = link_node.get(selectors.link_attribute)
        if not title:
            raise ValueError("公告項目缺少標題")
        if not isinstance(raw_href, str) or not raw_href.strip():
            raise ValueError("公告項目缺少連結屬性")

        published_at = parse_published_at(" ".join(date_node.get_text(" ", strip=True).split()))
        canonical_url = canonicalize_nptu_url(urljoin(self._config.url, raw_href.strip()))
        if not is_allowed_source_url(canonical_url, self._config.allowed_hosts):
            raise ValueError("公告詳細網址不在來源 host allowlist")
        return AnnouncementCandidate(
            title=title,
            canonical_url=canonical_url,
            unit=self._config.unit,
            category=self._config.category,
            published_at=published_at,
            deadline_at=None,
            body=title,
        )

    def parse_detail(self, content: str) -> str:
        selector = self._config.detail.content_selector if self._config.detail else None
        if selector is None:
            return extract_clean_html(content)
        soup = BeautifulSoup(content, "html.parser")
        root = soup.select_one(selector)
        if not isinstance(root, Tag):
            raise ValueError("找不到設定的公告詳細內容區塊")
        return extract_clean_html(str(root))
