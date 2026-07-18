from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from nptu_assistant.core.security import is_allowed_nptu_url, is_allowed_source_url


class CrawlHttpClient:
    def __init__(
        self,
        user_agent: str,
        *,
        interval_seconds: float = 1.0,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_redirects: int = 5,
        timeout_seconds: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("請求間隔不得小於零")
        if max_response_bytes < 1:
            raise ValueError("回應大小上限必須大於零")
        if max_redirects < 0:
            raise ValueError("redirect 上限不得小於零")
        if timeout_seconds <= 0:
            raise ValueError("HTTP timeout 必須大於零")
        self._user_agent = user_agent
        self._interval = interval_seconds
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._sleep = sleep
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds)),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def reset_robots(self) -> None:
        self._robots.clear()

    def get(self, url: str, *, allowed_hosts: Collection[str] | None = None) -> str:
        self._validate_url(url, allowed_hosts)
        self._ensure_allowed_by_robots(url, allowed_hosts)
        return self._request("get", url, allowed_hosts=allowed_hosts)

    def get_html(
        self, url: str, *, allowed_hosts: Collection[str] | None = None
    ) -> str:
        self._validate_url(url, allowed_hosts)
        self._ensure_allowed_by_robots(url, allowed_hosts)
        return self._request(
            "get",
            url,
            allowed_hosts=allowed_hosts,
            allowed_content_types=("text/html", "application/xhtml+xml"),
        )

    def submit_form(
        self,
        method: str,
        url: str,
        fields: Mapping[str, str],
        *,
        allowed_hosts: Collection[str] | None = None,
    ) -> str:
        normalized_method = method.lower()
        if normalized_method not in {"get", "post"}:
            raise ValueError("搜尋表單僅允許 GET 或 POST")
        self._validate_url(url, allowed_hosts)
        self._ensure_allowed_by_robots(url, allowed_hosts)
        return self._request(
            normalized_method,
            url,
            fields,
            allowed_hosts=allowed_hosts,
        )

    def _request(
        self,
        method: str,
        url: str,
        fields: Mapping[str, str] | None = None,
        *,
        allowed_hosts: Collection[str] | None = None,
        check_redirect_robots: bool = True,
        allowed_content_types: tuple[str, ...] | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                current_url = url
                current_method = method
                current_fields = fields
                for redirect_count in range(self._max_redirects + 1):
                    self._validate_url(current_url, allowed_hosts)
                    self._throttle(current_url)
                    with self._client.stream(
                        current_method.upper(),
                        current_url,
                        params=current_fields if current_method == "get" else None,
                        data=current_fields if current_method == "post" else None,
                    ) as response:
                        self._last_request[urlsplit(current_url).netloc.lower()] = (
                            time.monotonic()
                        )
                        if response.is_redirect:
                            if redirect_count >= self._max_redirects:
                                raise ValueError("redirect 次數超過安全上限")
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("redirect 回應缺少 Location")
                            target = urljoin(current_url, location)
                            self._validate_url(target, allowed_hosts)
                            if check_redirect_robots:
                                self._ensure_allowed_by_robots(target, allowed_hosts)
                            current_url = target
                            if current_method == "get":
                                current_fields = None
                            elif response.status_code == 303 or (
                                response.status_code in {301, 302}
                                and current_method == "post"
                            ):
                                current_method = "get"
                                current_fields = None
                            continue

                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").lower()
                        if allowed_content_types and not any(
                            content_type.startswith(value)
                            for value in allowed_content_types
                        ):
                            raise ValueError("官方來源回應不是支援的 HTML 格式")
                        content_length = response.headers.get("content-length")
                        if (
                            content_length
                            and int(content_length) > self._max_response_bytes
                        ):
                            raise ValueError("官方來源回應超過大小上限")
                        chunks: list[bytes] = []
                        received = 0
                        for chunk in response.iter_bytes():
                            received += len(chunk)
                            if received > self._max_response_bytes:
                                raise ValueError("官方來源回應超過大小上限")
                            chunks.append(chunk)
                        encoding = response.encoding or "utf-8"
                        return b"".join(chunks).decode(encoding, errors="replace")
            except (
                httpx.TimeoutException,
                httpx.TransportError,
                httpx.HTTPStatusError,
            ) as exc:
                last_error = exc
                if attempt < 2:
                    self._sleep(0.5 * (2**attempt))
        raise RuntimeError("官方來源連線失敗，已完成三次有限重試") from last_error

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < self._interval:
            self._sleep(self._interval - elapsed)

    @staticmethod
    def _validate_url(url: str, allowed_hosts: Collection[str] | None) -> None:
        if not is_allowed_nptu_url(url):
            raise ValueError("URL 在 NPTU allowlist 之外")
        if allowed_hosts is not None and not is_allowed_source_url(url, allowed_hosts):
            raise ValueError("URL 在來源 host allowlist 之外")

    def _ensure_allowed_by_robots(
        self,
        url: str,
        allowed_hosts: Collection[str] | None = None,
    ) -> None:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(origin)
        if parser is None:
            robots_url = f"{origin}/robots.txt"
            robots_text = self._request(
                "get",
                robots_url,
                allowed_hosts=allowed_hosts,
                check_redirect_robots=False,
            )
            parser = RobotFileParser(robots_url)
            parser.parse(robots_text.splitlines())
            self._robots[origin] = parser
        if not parser.can_fetch(self._user_agent, url):
            raise PermissionError("robots.txt 不允許爬取此 URL")
