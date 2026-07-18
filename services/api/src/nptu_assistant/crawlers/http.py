from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from nptu_assistant.core.security import is_allowed_nptu_url, is_allowed_source_url
from nptu_assistant.crawlers.site_models import (
    SearchDeadline,
    SearchDeadlineExceeded,
)


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
        clock: Callable[[], float] = time.monotonic,
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
        self._timeout_seconds = timeout_seconds
        self._sleep = sleep
        self._clock = clock
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

    def get(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        self._validate_url(url, allowed_hosts)
        self._ensure_allowed_by_robots(
            url,
            allowed_hosts,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        return self._request(
            "get",
            url,
            allowed_hosts=allowed_hosts,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )

    def get_html(
        self,
        url: str,
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        self._validate_url(url, allowed_hosts)
        self._ensure_allowed_by_robots(
            url,
            allowed_hosts,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        return self._request(
            "get",
            url,
            allowed_hosts=allowed_hosts,
            allowed_content_types=("text/html", "application/xhtml+xml"),
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )

    def submit_form(
        self,
        method: str,
        url: str,
        fields: Mapping[str, str],
        *,
        allowed_hosts: Collection[str] | None = None,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        normalized_method = method.lower()
        if normalized_method not in {"get", "post"}:
            raise ValueError("搜尋表單僅允許 GET 或 POST")
        self._validate_url(url, allowed_hosts)
        self._ensure_allowed_by_robots(
            url,
            allowed_hosts,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        return self._request(
            normalized_method,
            url,
            fields,
            allowed_hosts=allowed_hosts,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
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
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(3):
            if deadline is not None:
                deadline.raise_if_expired()
            try:
                current_url = url
                current_method = method
                current_fields = fields
                for redirect_count in range(self._max_redirects + 1):
                    if deadline is not None:
                        deadline.raise_if_expired()
                    self._validate_url(current_url, allowed_hosts)
                    self._throttle(current_url, deadline=deadline)
                    effective_timeout = self._effective_timeout(
                        timeout_seconds,
                        deadline,
                    )
                    with self._client.stream(
                        current_method.upper(),
                        current_url,
                        params=current_fields if current_method == "get" else None,
                        data=current_fields if current_method == "post" else None,
                        timeout=httpx.Timeout(
                            effective_timeout,
                            connect=min(5.0, effective_timeout),
                        ),
                    ) as response:
                        self._last_request[urlsplit(current_url).netloc.lower()] = (
                            self._clock()
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
                                self._ensure_allowed_by_robots(
                                    target,
                                    allowed_hosts,
                                    timeout_seconds=timeout_seconds,
                                    deadline=deadline,
                                )
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
                            if deadline is not None:
                                deadline.raise_if_expired()
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
                if deadline is not None and deadline.expired():
                    raise SearchDeadlineExceeded(
                        "官方來源請求耗盡網站搜尋時間額度"
                    ) from exc
                if attempt < 2:
                    self._sleep_bounded(0.5 * (2**attempt), deadline)
        raise RuntimeError("官方來源連線失敗，已完成三次有限重試") from last_error

    def _effective_timeout(
        self,
        timeout_seconds: float | None,
        deadline: SearchDeadline | None,
    ) -> float:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise SearchDeadlineExceeded("官方來源請求已無剩餘時間")
        values = [self._timeout_seconds]
        if timeout_seconds is not None:
            values.append(timeout_seconds)
        if deadline is not None:
            deadline.raise_if_expired()
            values.append(deadline.remaining_seconds())
        effective = min(values)
        if effective <= 0:
            raise SearchDeadlineExceeded("官方來源請求已無剩餘時間")
        return effective

    def _sleep_bounded(
        self,
        duration: float,
        deadline: SearchDeadline | None,
    ) -> None:
        if duration <= 0:
            return
        if deadline is None:
            self._sleep(duration)
            return
        deadline.raise_if_expired()
        self._sleep(min(duration, deadline.remaining_seconds()))
        deadline.raise_if_expired()

    def _throttle(self, url: str, *, deadline: SearchDeadline | None = None) -> None:
        host = urlsplit(url).netloc.lower()
        elapsed = self._clock() - self._last_request.get(host, 0.0)
        if elapsed < self._interval:
            self._sleep_bounded(self._interval - elapsed, deadline)

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
        *,
        timeout_seconds: float | None = None,
        deadline: SearchDeadline | None = None,
    ) -> None:
        if deadline is not None:
            deadline.raise_if_expired()
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
                timeout_seconds=timeout_seconds,
                deadline=deadline,
            )
            parser = RobotFileParser(robots_url)
            parser.parse(robots_text.splitlines())
            self._robots[origin] = parser
        if not parser.can_fetch(self._user_agent, url):
            raise PermissionError("robots.txt 不允許爬取此 URL")
