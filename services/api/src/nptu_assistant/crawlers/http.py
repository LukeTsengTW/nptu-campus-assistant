from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from nptu_assistant.core.security import is_allowed_nptu_url


class CrawlHttpClient:
    def __init__(
        self,
        user_agent: str,
        *,
        interval_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._interval = interval_seconds
        self._sleep = sleep
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def reset_robots(self) -> None:
        self._robots.clear()

    def get(self, url: str) -> str:
        if not is_allowed_nptu_url(url):
            raise ValueError("拒絕非 NPTU allowlist URL")
        self._ensure_allowed_by_robots(url)
        return self._request("get", url)

    def submit_form(self, method: str, url: str, fields: Mapping[str, str]) -> str:
        normalized_method = method.lower()
        if normalized_method not in {"get", "post"}:
            raise ValueError("搜尋表單僅允許 GET 或 POST")
        if not is_allowed_nptu_url(url):
            raise ValueError("拒絕非 NPTU allowlist URL")
        self._ensure_allowed_by_robots(url)
        return self._request(normalized_method, url, fields)

    def _request(
        self,
        method: str,
        url: str,
        fields: Mapping[str, str] | None = None,
    ) -> str:
        host = urlsplit(url).netloc.lower()
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < self._interval:
            self._sleep(self._interval - elapsed)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.request(
                    method.upper(),
                    url,
                    params=fields if method == "get" else None,
                    data=fields if method == "post" else None,
                )
                response.raise_for_status()
                if not is_allowed_nptu_url(str(response.url)):
                    raise ValueError("redirect target is outside the NPTU allowlist")
                self._last_request[host] = time.monotonic()
                return response.text
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt < 2:
                    self._sleep(0.5 * (2**attempt))
        raise RuntimeError("官方來源連線失敗，已完成三次有限重試") from last_error

    def _ensure_allowed_by_robots(self, url: str) -> None:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            robots_url = f"{origin}/robots.txt"
            robots_text = self._request("get", robots_url)
            parser = RobotFileParser(robots_url)
            parser.parse(robots_text.splitlines())
            self._robots[origin] = parser
        if not parser.can_fetch(self._user_agent, url):
            raise PermissionError("robots.txt 不允許爬取此 URL")
