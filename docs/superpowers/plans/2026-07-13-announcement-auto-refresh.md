# 最新公告自動刷新實作計畫

> **給 agentic worker：** 必須逐項使用 `superpowers:test-driven-development` 執行本計畫；本 task 預設使用 `superpowers:executing-plans` inline 執行。除非使用者明確要求，不派遣 subagent。每一步使用 checkbox 追蹤。

**目標：** 使用者查詢最新公告前，依 Allowlist 刷新國立屏東大學前 20 則公告；背景工作亦讓 `crawl_interval_minutes: 60` 真正控制每小時刷新。

**架構：** 新增共用 `AnnouncementRefreshCoordinator`，以來源設定、最後爬取時間及 process-local lock 控制 freshness。聊天 `search_announcements(sort="newest")` 與 FastAPI 背景 scheduler 共用 coordinator；所有回答仍在刷新完成後從資料庫查詢，爬取失敗時使用最後成功資料並附固定警告。

**技術棧：** Python 3.12、FastAPI lifespan、SQLAlchemy 2、Pydantic 2、httpx、pytest、PostgreSQL。

## 全域限制

- 所有使用者可見文字使用繁體中文。
- 爬蟲只接受 `data/sources/announcements.yaml` 內已驗證的 NPTU HTTPS 來源。
- Extension 不新增權限、秘密金鑰或資料蒐集。
- 正式回答的 URL 必須來自資料庫 evidence。
- 公告完全重複時不得新增；同 URL 官方內容變更時更新原列。
- 無資料時回覆「目前收錄的官方資料不足以確認。」
- 每個新行為先寫測試、確認失敗，再寫最小實作。
- 不忽略、刪除或弱化既有測試。
- 本設計不變更資料庫 schema，不建立 Migration。

---

## 檔案結構

- 新增 `services/api/src/nptu_assistant/crawlers/refresh.py`：freshness gate、刷新結果、背景 scheduler。
- 修改 `services/api/src/nptu_assistant/db/repositories.py`：查詢來源最後公告爬取時間。
- 修改 `services/api/src/nptu_assistant/rag/tools.py`：最新公告工具執行前刷新、傳遞穩定警告。
- 修改 `services/api/src/nptu_assistant/rag/service.py`：將工具警告寫入 `ChatResponse.warning`。
- 修改 `services/api/src/nptu_assistant/wiring.py`：建立並共享 crawler、coordinator、scheduler。
- 修改 `services/api/src/nptu_assistant/main.py`：FastAPI lifespan 啟停 scheduler。
- 修改 `data/sources/announcements.yaml`：明確限制 `max_items: 20`，保留 60 分鐘間隔。
- 新增 `services/api/tests/test_refresh.py`：coordinator 與 scheduler 單元測試。
- 修改 `services/api/tests/test_chat_orchestration.py`：工具刷新與警告回歸測試。
- 修改 `services/api/tests/test_project_config.py`：正式來源上限及間隔設定測試。
- 修改 `services/api/tests/test_api.py`：lifespan scheduler 注入與停止測試。
- 修改 `README.md`、`docs/data-sources.md`：操作語意、失敗降級、單 process 鎖限制。

---

### Task 1：來源 freshness 與單次刷新協調器

**檔案：**

- 新增：`services/api/src/nptu_assistant/crawlers/refresh.py`
- 修改：`services/api/src/nptu_assistant/db/repositories.py:120`
- 修改：`data/sources/announcements.yaml:2`
- 新增測試：`services/api/tests/test_refresh.py`
- 修改測試：`services/api/tests/test_project_config.py`

**介面：**

- 輸入：`CrawlerService.run(source_names: list[str] | None) -> CrawlSummary`
- 輸入：`CrawlerSourceConfig.crawl_interval_minutes`
- 產出：`SqlAnnouncementRepository.latest_crawled_at(source_name: str) -> datetime | None`
- 產出：`AnnouncementRefreshCoordinator.ensure_fresh(source_name: str) -> RefreshResult`
- 產出：`AnnouncementRefreshCoordinator.refresh_due_sources() -> list[RefreshResult]`

- [ ] **Step 1：建立 freshness RED 測試**

在 `services/api/tests/test_refresh.py` 建立固定時鐘、fake repository、fake crawler，加入以下行為測試：

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.refresh import AnnouncementRefreshCoordinator


NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


class MemoryFreshnessRepository:
    def __init__(self, last_crawled_at: datetime | None) -> None:
        self.last_crawled_at_value = last_crawled_at

    def latest_crawled_at(self, source_name: str) -> datetime | None:
        assert source_name == "nptu-overview"
        return self.last_crawled_at_value


class RecordingCrawler:
    def __init__(self, summary: CrawlSummary | None = None) -> None:
        self.summary = summary or CrawlSummary(unchanged=20)
        self.calls: list[list[str] | None] = []

    def run(self, source_names: list[str] | None = None) -> CrawlSummary:
        self.calls.append(source_names)
        return self.summary


def write_config(path: Path) -> None:
    path.write_text(
        """sources:
  - name: nptu-overview
    adapter: nptu_overview
    url: https://www.nptu.edu.tw/p/503-1000-1044.php?Lang=zh-tw
    unit: 國立屏東大學
    category: 總覽
    enabled: true
    crawl_interval_minutes: 60
    max_items: 20
""",
        encoding="utf-8",
    )


def test_fresh_source_skips_crawl(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler()
    coordinator = AnnouncementRefreshCoordinator(
        config,
        crawler,
        MemoryFreshnessRepository(NOW - timedelta(minutes=59)),
        now=lambda: NOW,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.attempted is False
    assert result.succeeded is True
    assert crawler.calls == []


def test_due_source_crawls_once_then_stays_fresh_in_memory(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler()
    coordinator = AnnouncementRefreshCoordinator(
        config,
        crawler,
        MemoryFreshnessRepository(NOW - timedelta(minutes=60)),
        now=lambda: NOW,
    )

    first = coordinator.ensure_fresh("nptu-overview")
    second = coordinator.ensure_fresh("nptu-overview")

    assert first.attempted is True
    assert first.succeeded is True
    assert second.attempted is False
    assert crawler.calls == [["nptu-overview"]]


def test_failed_refresh_returns_stable_warning(tmp_path: Path) -> None:
    config = tmp_path / "announcements.yaml"
    write_config(config)
    crawler = RecordingCrawler(CrawlSummary(failed=1, errors=["HTTP 503"]))
    coordinator = AnnouncementRefreshCoordinator(
        config,
        crawler,
        MemoryFreshnessRepository(None),
        now=lambda: NOW,
    )

    result = coordinator.ensure_fresh("nptu-overview")

    assert result.succeeded is False
    assert result.warning == "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
```

在 `services/api/tests/test_project_config.py` 加入：

```python
def test_live_announcement_source_uses_twenty_items_and_hourly_refresh() -> None:
    payload = yaml.safe_load((WORKSPACE_ROOT / "data/sources/announcements.yaml").read_text(encoding="utf-8"))
    source = next(item for item in payload["sources"] if item["name"] == "nptu-overview")

    assert source["max_items"] == 20
    assert source["crawl_interval_minutes"] == 60
```

- [ ] **Step 2：執行 RED 測試**

執行：

```powershell
services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_refresh.py services\api\tests\test_project_config.py -q
```

預期：`ModuleNotFoundError: No module named 'nptu_assistant.crawlers.refresh'`，或缺少 `max_items` assertion failure。確認失敗原因是功能尚未存在。

- [ ] **Step 3：加入 SQL freshness 查詢**

在 `SqlAnnouncementRepository` 加入：

```python
def latest_crawled_at(self, source_name: str) -> datetime | None:
    with self._factory() as session:
        return session.scalar(
            select(func.max(Announcement.last_crawled_at))
            .join(Source, Source.id == Announcement.source_id)
            .where(Source.name == source_name)
        )
```

確認 `repositories.py` 的 datetime import 為：

```python
from datetime import date, datetime, timezone
```

- [ ] **Step 4：建立最小 coordinator**

`services/api/src/nptu_assistant/crawlers/refresh.py` 完整內容：

```python
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from nptu_assistant.api.schemas import CrawlSummary
from nptu_assistant.crawlers.config import CrawlerSourceConfig, load_source_configs


logger = logging.getLogger(__name__)
REFRESH_FAILURE_WARNING = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"


class CrawlRunner(Protocol):
    def run(self, source_names: list[str] | None = None) -> CrawlSummary:
        raise NotImplementedError


class FreshnessRepository(Protocol):
    def latest_crawled_at(self, source_name: str) -> datetime | None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class RefreshResult:
    source_name: str
    attempted: bool
    succeeded: bool
    warning: str | None = None
    summary: CrawlSummary | None = None


class AnnouncementRefreshCoordinator:
    def __init__(
        self,
        config_path: Path,
        crawler: CrawlRunner,
        repository: FreshnessRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._config_path = config_path
        self._crawler = crawler
        self._repository = repository
        self._now = now
        self._lock = threading.Lock()
        self._last_success: dict[str, datetime] = {}

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        config = self._config(source_name)
        with self._lock:
            checked_at = self._now()
            if not self._is_due(config, checked_at):
                return RefreshResult(source_name, attempted=False, succeeded=True)
            summary = self._crawler.run([source_name])
            if summary.failed:
                return RefreshResult(
                    source_name,
                    attempted=True,
                    succeeded=False,
                    warning=REFRESH_FAILURE_WARNING,
                    summary=summary,
                )
            self._last_success[source_name] = checked_at
            return RefreshResult(
                source_name,
                attempted=True,
                succeeded=True,
                summary=summary,
            )

    def refresh_due_sources(self) -> list[RefreshResult]:
        return [
            self.ensure_fresh(config.name)
            for config in load_source_configs(self._config_path)
            if config.enabled
        ]

    def _config(self, source_name: str) -> CrawlerSourceConfig:
        configs = {item.name: item for item in load_source_configs(self._config_path)}
        config = configs.get(source_name)
        if config is None or not config.enabled:
            raise ValueError(f"未知或未啟用的 crawler source：{source_name}")
        return config

    def _is_due(self, config: CrawlerSourceConfig, checked_at: datetime) -> bool:
        timestamps = [
            value
            for value in (
                self._repository.latest_crawled_at(config.name),
                self._last_success.get(config.name),
            )
            if value is not None
        ]
        if not timestamps:
            return True
        return checked_at - max(timestamps) >= timedelta(minutes=config.crawl_interval_minutes)
```

先不要加入 scheduler；保持本 task 單一行為。

- [ ] **Step 5：設定正式來源最多 20 則**

在 `nptu-overview` 加入：

```yaml
    crawl_interval_minutes: 60
    max_items: 20
```

- [ ] **Step 6：執行 GREEN 測試**

執行 Task 1 測試命令。預期全部通過。

- [ ] **Step 7：補並行回歸測試**

使用 `ThreadPoolExecutor(max_workers=2)` 同時呼叫兩次 `ensure_fresh`；fake crawler 以 `threading.Event` 控制第一個呼叫停留。斷言 crawler 只收到一次 `["nptu-overview"]`。先確認測試在移除 lock 時失敗，再恢復 lock 並確認通過。

- [ ] **Step 8：提交 Task 1**

```powershell
git add data/sources/announcements.yaml services/api/src/nptu_assistant/crawlers/refresh.py services/api/src/nptu_assistant/db/repositories.py services/api/tests/test_refresh.py services/api/tests/test_project_config.py
git commit -m "feat(crawler): gate announcement refresh by source interval"
```

---

### Task 2：最新公告工具刷新與失敗降級

**檔案：**

- 修改：`services/api/src/nptu_assistant/rag/tools.py:130`
- 修改：`services/api/src/nptu_assistant/rag/service.py:75`
- 修改測試：`services/api/tests/test_chat_orchestration.py`

**介面：**

- 輸入：`AnnouncementRefreshCoordinator.ensure_fresh("nptu-overview")`
- 產出：`ToolExecutionResult.warning: str | None`
- 產出：`ChatResponse.warning` 必定包含 refresh failure warning。

- [ ] **Step 1：寫工具刷新 RED 測試**

在 `test_chat_orchestration.py` 新增 fake refresher：

```python
class RecordingRefresher:
    def __init__(self, warning: str | None = None) -> None:
        self.warning = warning
        self.calls: list[str] = []

    def ensure_fresh(self, source_name: str) -> RefreshResult:
        self.calls.append(source_name)
        return RefreshResult(
            source_name=source_name,
            attempted=True,
            succeeded=self.warning is None,
            warning=self.warning,
        )
```

加入三個測試：

```python
def test_newest_announcement_tool_refreshes_before_database_search() -> None:
    retriever = StubRetriever({"search_announcements": [evidence()]})
    refresher = RecordingRefresher()
    executor = ToolExecutor(retriever, refresher)

    result = executor.execute("search_announcements", json.dumps(announcement_args()))

    assert refresher.calls == ["nptu-overview"]
    assert len(result.evidence) == 1


def test_non_newest_announcement_tool_does_not_request_refresh() -> None:
    arguments = announcement_args()
    arguments["sort"] = "relevance"
    refresher = RecordingRefresher()
    executor = ToolExecutor(StubRetriever({"search_announcements": []}), refresher)

    executor.execute("search_announcements", json.dumps(arguments))

    assert refresher.calls == []


def test_refresh_failure_keeps_database_evidence_and_returns_warning() -> None:
    warning = "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
    executor = ToolExecutor(
        StubRetriever({"search_announcements": [evidence()]}),
        RecordingRefresher(warning),
    )

    result = executor.execute("search_announcements", json.dumps(announcement_args()))

    assert len(result.evidence) == 1
    assert result.warning == warning
    assert json.loads(result.output)["warning"] == warning
```

- [ ] **Step 2：執行 RED 測試**

```powershell
services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_chat_orchestration.py -q
```

預期：`ToolExecutor.__init__()` 不接受 refresher，或 `ToolExecutionResult` 無 `warning`。

- [ ] **Step 3：修改 `ToolExecutor`**

在 `tools.py` 加入：

```python
from typing import Protocol

from nptu_assistant.crawlers.refresh import RefreshResult


class AnnouncementRefresher(Protocol):
    def ensure_fresh(self, source_name: str) -> RefreshResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    output: str
    evidence: list[Evidence]
    warning: str | None = None
```

建構子及公告執行分支改為：

```python
class ToolExecutor:
    def __init__(
        self,
        retriever: StructuredRetriever,
        refresher: AnnouncementRefresher | None = None,
    ) -> None:
        self._retriever = retriever
        self._refresher = refresher

    def _refresh_warning(self, parsed: SearchAnnouncementsArguments) -> str | None:
        if parsed.sort is not AnnouncementSort.NEWEST or self._refresher is None:
            return None
        try:
            return self._refresher.ensure_fresh("nptu-overview").warning
        except Exception:
            return "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
```

在 `execute` 的 `SearchAnnouncementsArguments` 分支先取得 `refresh_warning`，資料庫查詢後將 payload 與 result 設成：

```python
payload = {
    "results": [
        _serialize_evidence(item, content_limit=content_limit) for item in evidence
    ],
    "count": len(evidence),
    "warning": refresh_warning,
}
return ToolExecutionResult(
    output=json.dumps(payload, ensure_ascii=False),
    evidence=evidence,
    warning=refresh_warning,
)
```

文件及公告 detail 工具的 `refresh_warning` 初始化為 `None`。

- [ ] **Step 4：讓 `ChatService` 合併工具警告**

`ChatService.__init__` 增加可選 refresher，初始化：

```python
self._tool_executor = ToolExecutor(retriever, announcement_refresher)
```

`answer` 建立 `tool_warnings: list[str]`。每次 tool result 若有 warning 且未重複便加入。呼叫 `_build_response` 時傳入 `tool_warnings`。

`_build_response` 在 sanitize LLM warning 後合併：

```python
warning_parts = [item for item in [warning, *tool_warnings] if item]
warning = "\n".join(dict.fromkeys(warning_parts)) or None
```

若 `used` 為空，維持既有 insufficient 分支優先；不可用 refresh warning 取代資料不足。

- [ ] **Step 5：新增 ChatResponse 警告測試**

建立一次 `search_announcements` tool call、舊 DB evidence、refresher failure。斷言：

```python
assert response.answer == "依據資料庫中的公告內容。"
assert response.warning == "最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。"
assert response.sources[0].url == "https://www.nptu.edu.tw/source/announcement-1"
```

另測無 evidence 時 `answer_type` 仍是 `INSUFFICIENT_INFORMATION`，回答仍是 `INSUFFICIENT_ANSWER`。

- [ ] **Step 6：執行 GREEN 與相關回歸測試**

```powershell
services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_chat_orchestration.py services\api\tests\test_rag.py services\api\tests\test_providers.py -q
```

預期全部通過，且無 warning 或 error output。

- [ ] **Step 7：提交 Task 2**

```powershell
git add services/api/src/nptu_assistant/rag/tools.py services/api/src/nptu_assistant/rag/service.py services/api/tests/test_chat_orchestration.py
git commit -m "feat(chat): refresh due announcements before newest search"
```

---

### Task 3：背景 scheduler 與 FastAPI lifespan

**檔案：**

- 修改：`services/api/src/nptu_assistant/crawlers/refresh.py`
- 修改：`services/api/src/nptu_assistant/wiring.py:30`
- 修改：`services/api/src/nptu_assistant/main.py:89`
- 修改測試：`services/api/tests/test_refresh.py`
- 修改測試：`services/api/tests/test_api.py`

**介面：**

- 產出：`AnnouncementRefreshScheduler.run() -> None`
- 產出：`AnnouncementRefreshScheduler.stop() -> None`
- 產出：`build_services()["refresh_scheduler"]`
- `create_app` 新增 `refresh_scheduler: Any | None = None` keyword，在 lifespan 啟停工作。

- [ ] **Step 1：寫 scheduler RED 測試**

在 `test_refresh.py` 加入：

```python
import asyncio

from nptu_assistant.crawlers.refresh import AnnouncementRefreshScheduler


class RecordingCoordinator:
    def __init__(self) -> None:
        self.calls = 0

    def refresh_due_sources(self) -> list[RefreshResult]:
        self.calls += 1
        return []


def test_scheduler_checks_immediately_and_stops_cleanly() -> None:
    async def exercise() -> None:
        coordinator = RecordingCoordinator()
        scheduler = AnnouncementRefreshScheduler(coordinator, check_interval_seconds=0.01)
        task = asyncio.create_task(scheduler.run())
        while coordinator.calls < 2:
            await asyncio.sleep(0.005)
        scheduler.stop()
        await asyncio.wait_for(task, timeout=1)
        assert coordinator.calls >= 2

    asyncio.run(exercise())
```

- [ ] **Step 2：執行 scheduler RED 測試**

執行 `test_refresh.py`。預期 import failure，因 scheduler 尚未存在。

- [ ] **Step 3：實作 scheduler**

加到 `refresh.py`：

```python
class AnnouncementRefreshScheduler:
    def __init__(
        self,
        coordinator: AnnouncementRefreshCoordinator,
        *,
        check_interval_seconds: float = 60.0,
    ) -> None:
        self._coordinator = coordinator
        self._check_interval_seconds = check_interval_seconds
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._coordinator.refresh_due_sources)
            except Exception:
                logger.exception("announcement_refresh_scheduler_failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._check_interval_seconds,
                )
            except TimeoutError:
                continue
```

- [ ] **Step 4：重構 wiring，共享單一 coordinator**

在 `build_services` 先建立 `crawler_service`：

```python
crawler_service = CrawlerService(
    resolve_workspace_path(settings.crawler_config_path),
    announcement_repository,
    http_client,
    workspace_root=WORKSPACE_ROOT,
)
announcement_refresher = AnnouncementRefreshCoordinator(
    resolve_workspace_path(settings.crawler_config_path),
    crawler_service,
    announcement_repository,
)
refresh_scheduler = AnnouncementRefreshScheduler(announcement_refresher)
```

`ChatService` 第四個參數傳入 `announcement_refresher`。services dict 回傳同一個 `crawler_service` 及 `refresh_scheduler`。不得建立第二個 HTTP client 或第二個 crawler。

- [ ] **Step 5：寫 FastAPI lifespan RED 測試**

在 `test_api.py` 建立：

```python
class StubScheduler:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self._stop = asyncio.Event()

    async def run(self) -> None:
        self.started = True
        await self._stop.wait()

    def stop(self) -> None:
        self.stopped = True
        self._stop.set()


def test_app_lifespan_starts_and_stops_refresh_scheduler() -> None:
    scheduler = StubScheduler()
    settings = Settings(
        _env_file=None,
        admin_api_enabled=True,
        admin_api_key="test-admin-key",
        cors_allowed_origins="http://localhost:3000",
        openai_api_key=None,
    )
    app = create_app(
        settings=settings,
        health_service=StubHealth(),
        chat_service=StubChat(),
        announcement_service=StubAnnouncements(),
        ingestion_service=StubOperation(),
        crawler_service=StubOperation(),
        refresh_scheduler=scheduler,
    )

    with TestClient(app):
        assert scheduler.started is True

    assert scheduler.stopped is True
```

- [ ] **Step 6：實作 FastAPI lifespan**

`main.py` 加入 `asyncio`、`asynccontextmanager` import；`create_app` 增加 `refresh_scheduler` keyword。只有 production defaults 建立時才自動取用 `defaults["refresh_scheduler"]`；測試完整注入服務但未提供 scheduler 時不得啟動背景工作。

lifespan closure：

```python
@asynccontextmanager
async def lifespan(_: FastAPI):
    if refresh_scheduler is None:
        yield
        return
    task = asyncio.create_task(refresh_scheduler.run())
    try:
        yield
    finally:
        refresh_scheduler.stop()
        await task


app = FastAPI(
    title="NPTU 校務資訊助理 API",
    version="0.1.0",
    lifespan=lifespan,
)
```

- [ ] **Step 7：執行 GREEN 與 API 回歸測試**

```powershell
services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_refresh.py services\api\tests\test_api.py services\api\tests\test_services.py -q
```

預期全部通過。確認測試結束後沒有殘留 task 或 `Task was destroyed but it is pending`。

- [ ] **Step 8：提交 Task 3**

```powershell
git add services/api/src/nptu_assistant/crawlers/refresh.py services/api/src/nptu_assistant/wiring.py services/api/src/nptu_assistant/main.py services/api/tests/test_refresh.py services/api/tests/test_api.py
git commit -m "feat(crawler): schedule hourly announcement refresh"
```

---

### Task 4：文件、全套驗證、需求核對

**檔案：**

- 修改：`README.md:64`
- 修改：`docs/data-sources.md`

**產出：**

- 操作者理解背景排程、request-time freshness、20 則上限、失敗降級。
- 全套後端測試與格式檢查具新鮮證據。

- [ ] **Step 1：更新操作文件**

README「執行公告爬蟲」補充：

```markdown
API 啟動後會每 60 秒檢查已啟用來源是否到期；`nptu-overview` 的實際刷新間隔由 `crawl_interval_minutes: 60` 控制。使用者查詢最新公告時也會先做相同檢查。未到期不會重新請求官網；到期時最多處理 RSS 前 20 則。

刷新失敗時，系統保留最後成功收錄的資料並在回答附上警告。所有回答來源仍從資料庫產生，模型不能指定任意爬取 URL。
```

`docs/data-sources.md` 記錄 process-local lock 限制：目前單一 API process 可避免重複爬取；部署多 worker 前必須改用 PostgreSQL advisory lock 或獨立 scheduler worker。

- [ ] **Step 2：執行 targeted tests**

```powershell
services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_refresh.py services\api\tests\test_crawler.py services\api\tests\test_services.py services\api\tests\test_chat_orchestration.py services\api\tests\test_rag.py services\api\tests\test_providers.py services\api\tests\test_api.py services\api\tests\test_project_config.py -q
```

要求 exit code 0；任何失敗先修復，不得跳過。

- [ ] **Step 3：執行完整 Python suite**

```powershell
services\api\.venv\Scripts\python.exe -m pytest services\api\tests -q
```

要求 exit code 0、0 failed。記錄 passed、skipped 數量；不得把 skipped 當 passed。

- [ ] **Step 4：檢查 diff 與敏感邊界**

```powershell
git diff --check
rg -n "https?://" services\api\src\nptu_assistant\crawlers services\api\src\nptu_assistant\rag
git status --short
```

人工確認：

- 新增 URL 僅為既有 NPTU Allowlist 設定或測試資料。
- `search_announcements` 回答 URL 仍來自 `Evidence.url`。
- 無 Chrome 權限變更。
- 無秘密、Cookie、學號、成績或身分證字號蒐集。
- 無資料庫 schema 變更；因此沒有漏建 Migration。
- `canonical_url` unique 與既有 upsert 去重未被削弱。

- [ ] **Step 5：提交文件**

```powershell
git add README.md docs/data-sources.md
git commit -m "docs(crawler): explain automatic announcement refresh"
```

- [ ] **Step 6：完成前再次驗證**

最後一次重新執行完整 Python suite 與 `git diff --check`。只依本次輸出回報完成狀態；不得引用較早測試結果。

## 完成條件

- Prompt「幫我查最新公告」導致 `search_announcements(sort="newest", limit<=20)`。
- 來源超過 60 分鐘未成功刷新時，先爬取官方 RSS 前 20 則。
- 60 分鐘內重複 Prompt 不再爬取。
- 背景排程在服務啟動後自動維持相同 60 分鐘 freshness。
- 相同公告不新增；官方內容改變才更新原列。
- 爬取失敗仍可回傳舊 DB 資料，並附固定警告。
- 無舊資料時明確回覆資料不足。
- 回答來源 URL 全部來自資料庫。
- targeted tests、完整 Python suite、`git diff --check` 全部通過。

## 資料來源

- 國立屏東大學最新消息總覽：https://www.nptu.edu.tw/p/422-1000-1044.php?Lang=zh-tw
- 國立屏東大學 RSS：https://www.nptu.edu.tw/p/503-1000-1044.php?Lang=zh-tw
- 國立屏東大學 robots.txt：https://www.nptu.edu.tw/robots.txt
- `docs/superpowers/specs/2026-07-13-announcement-auto-refresh-design.md`
- `data/sources/announcements.yaml`
- `services/api/src/nptu_assistant/crawlers/service.py`
- `services/api/src/nptu_assistant/db/repositories.py`
- `services/api/src/nptu_assistant/rag/tools.py`
- `services/api/src/nptu_assistant/rag/service.py`
