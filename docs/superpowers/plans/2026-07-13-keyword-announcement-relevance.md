# Keyword Announcement Relevance Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓非空公告查詢只從本次官網搜尋取得的 canonical URL 對應 DB 公告產生 Evidence，再依 `newest`、`oldest` 或 `relevance` 排序。

**Architecture:** `KeywordAnnouncementSearchService` 在完成 upsert 後回傳本次成功收錄的 URL 範圍；`ToolExecutor` 將該範圍作為不可由 LLM 控制的內部參數傳給 `SqlRetriever`；Retriever 先以 URL 範圍限制候選，再套用既有篩選與排序。全部官網搜尋失敗時以 `None` 表示沿用 DB fallback，搜尋成功但零結果時以空 tuple 表示必須回傳空 Evidence。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy 2、PostgreSQL/pg_trgm、Pydantic、pytest、httpx。

## Global Constraints

- 所有使用者可見文字使用繁體中文。
- 正式回答的來源 URL 只能來自 DB Evidence。
- 不新增嚴格單位篩選；其他單位發布但內容相關的公告仍可出現。
- 不修改 `/v1/chat`、工具 JSON schema、Extension、Chrome 權限或 shared schema。
- 不新增資料庫欄位，因此不建立 Migration。
- 每項 production 行為必須先有會正確失敗的回歸測試。

---

### Task 1: 搜尋服務回傳本次成功收錄的 canonical URL 範圍

**Files:**
- Modify: `services/api/src/nptu_assistant/crawlers/search.py`
- Test: `services/api/tests/test_keyword_search.py`
- Test: `services/api/tests/test_live_keyword_search.py`

**Interfaces:**
- Consumes: `AnnouncementRepository.upsert(...) -> str`，回傳 `created`、`updated` 或 `unchanged`。
- Produces: `KeywordIngestionResult.canonical_urls: tuple[str, ...] | None`。

- [ ] **Step 1: 寫入 canonical URL 範圍的失敗測試**

在 `test_keyword_search_service_submits_variants_deduplicates_and_ingests` 增加：

```python
assert result.canonical_urls == (
    "https://csai.nptu.edu.tw/p/406-1096-197001.php?Lang=zh-tw",
)
```

在 `test_keyword_search_service_reports_partial_and_total_failures` 增加：

```python
assert partial.canonical_urls == (
    "https://csai.nptu.edu.tw/p/406-1096-197001.php?Lang=zh-tw",
)
assert failed.canonical_urls is None
```

新增搜尋成功但零結果測試：

```python
def test_keyword_search_service_returns_empty_scope_for_successful_empty_search() -> None:
    class EmptySearchHttpClient(SearchHttpClient):
        def submit_form(self, method: str, url: str, fields: dict[str, str]) -> str:
            if "Action=mobileloadmod" in url:
                return BOOTSTRAP_FIXTURE.read_text(encoding="utf-8")
            return FORM_FIXTURE.read_text(encoding="utf-8") + '<div data-search-results></div>'

    result = KeywordAnnouncementSearchService(
        keyword_config(aliases={}),
        MemoryAnnouncementRepository(),
        EmptySearchHttpClient(),
    ).ingest("查無結果關鍵字")

    assert result.canonical_urls == ()
    assert result.warning is None
```

- [ ] **Step 2: 執行測試確認 RED**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_keyword_search.py -q
```

Expected: FAIL，`KeywordIngestionResult` 尚無 `canonical_urls`。

- [ ] **Step 3: 實作最小 canonical URL 收集**

將結果型別擴充為：

```python
@dataclass(frozen=True, slots=True)
class KeywordIngestionResult:
    retrieval_query: str
    summary: CrawlSummary
    warning: str | None = None
    canonical_urls: tuple[str, ...] | None = None
```

在 `ingest` 處理 details 前建立 `ingested_urls: list[str] = []`；每次 `upsert` 正常回傳後加入：

```python
outcome = self._repository.upsert(...)
ingested_urls.append(result.canonical_url)
```

最後回傳：

```python
return KeywordIngestionResult(
    expansion.retrieval_query,
    summary,
    warning,
    tuple(ingested_urls) if successful_searches else None,
)
```

bootstrap 失敗的早期回傳維持預設 `canonical_urls=None`。

- [ ] **Step 4: 強化 opt-in live smoke 非空契約**

在 `test_live_keyword_search_form_and_result_contract` 的 URL assertion 前加入：

```python
assert results
assert all(result.canonical_url.startswith("https://") for result in results)
```

- [ ] **Step 5: 執行 Task 1 測試確認 GREEN**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_keyword_search.py services\api\tests\test_live_keyword_search.py -q
```

Expected: 一般測試 PASS；live smoke 未 opt-in 時顯示 1 skipped。

- [ ] **Step 6: 提交 Task 1**

```powershell
git add services/api/src/nptu_assistant/crawlers/search.py services/api/tests/test_keyword_search.py services/api/tests/test_live_keyword_search.py
git commit -m "fix(crawler): retain keyword result URL scope"
```

---

### Task 2: ToolExecutor 將 URL 範圍傳給內部 Retriever

**Files:**
- Modify: `services/api/src/nptu_assistant/rag/tools.py`
- Test: `services/api/tests/test_tools.py`

**Interfaces:**
- Consumes: `KeywordIngestionResult.canonical_urls`。
- Produces: `StructuredRetriever.search_announcements(..., canonical_urls: tuple[str, ...] | None)`。

- [ ] **Step 1: 寫入 ToolExecutor 傳遞範圍的失敗測試**

讓 `StubKeywordIngestor.ingest` 回傳：

```python
return KeywordIngestionResult(
    retrieval_query=self.normalize(query),
    summary=CrawlSummary(created=1),
    warning=None,
    canonical_urls=("https://www.nptu.edu.tw/p/406-1000-200001.php",),
)
```

在 `test_executor_ingests_keyword_before_database_search_and_normalizes_filters` 的預期參數加入：

```python
"canonical_urls": ("https://www.nptu.edu.tw/p/406-1000-200001.php",),
```

在 ingestion failure 與 `query=None` 測試分別確認：

```python
assert retriever.calls[0][1]["canonical_urls"] is None
assert retriever.calls[1][1]["canonical_urls"] is None
```

- [ ] **Step 2: 執行測試確認 RED**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_tools.py -q
```

Expected: FAIL，retriever arguments 尚無 `canonical_urls`。

- [ ] **Step 3: 擴充內部 protocol 與 ToolExecutor**

在 `StructuredRetriever.search_announcements` 加入：

```python
canonical_urls: tuple[str, ...] | None = None,
```

在 `_search_announcements` 初始化內部參數：

```python
arguments = parsed.model_dump()
arguments["canonical_urls"] = None
```

ingestion 成功後加入：

```python
arguments["canonical_urls"] = ingestion.canonical_urls
```

這個欄位不得加入 `SearchAnnouncementsArguments` 或 `tool_definitions()`。

- [ ] **Step 4: 執行 Task 2 測試確認 GREEN**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_tools.py services\api\tests\test_chat_orchestration.py -q
```

Expected: PASS，且公開工具 schema 測試保持不變。

- [ ] **Step 5: 提交 Task 2**

```powershell
git add services/api/src/nptu_assistant/rag/tools.py services/api/tests/test_tools.py
git commit -m "fix(rag): pass keyword URL scope to retrieval"
```

---

### Task 3: SQL Retriever 先限制 URL 候選再排序

**Files:**
- Modify: `services/api/src/nptu_assistant/rag/retrieval.py`
- Test: `services/api/tests/test_retrieval.py`

**Interfaces:**
- Consumes: `canonical_urls: tuple[str, ...] | None`。
- Produces: 只從指定 URL 建立的 `list[Evidence]`；空 tuple 直接回傳空 list。

- [ ] **Step 1: 寫入 newest 相關範圍的失敗測試**

新增：

```python
def test_newest_keyword_search_is_limited_to_current_canonical_urls() -> None:
    session = FakeSession()
    urls = (
        "https://www.nptu.edu.tw/p/406-1000-200001.php",
        "https://csai.nptu.edu.tw/p/406-1096-200002.php",
    )

    make_retriever(session).search_announcements(
        query="電腦科學與人工智慧學系",
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=None,
        date_from=None,
        date_to=None,
        canonical_urls=urls,
    )

    statement = sql(session.statements[0])
    assert "announcements.canonical_url IN" in statement
    assert urls[0] in statement
    assert urls[1] in statement
    assert "ORDER BY announcements.published_at DESC" in statement
```

新增空範圍測試：

```python
def test_successful_empty_keyword_scope_returns_no_announcements() -> None:
    session = FakeSession()

    result = make_retriever(session).search_announcements(
        query="查無結果關鍵字",
        limit=5,
        sort=AnnouncementSort.NEWEST,
        unit=None,
        date_from=None,
        date_to=None,
        canonical_urls=(),
    )

    assert result == []
    assert session.statements == []
```

- [ ] **Step 2: 執行測試確認 RED**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_retrieval.py -q
```

Expected: FAIL，`search_announcements` 尚不接受 `canonical_urls`。

- [ ] **Step 3: 實作 URL 範圍過濾**

在 `SqlRetriever.search_announcements` 的 keyword-only 參數尾端加入：

```python
canonical_urls: tuple[str, ...] | None = None,
```

在建立 score 與 statement 前處理空集合：

```python
if canonical_urls == ():
    return []
```

在 `filters` 加入：

```python
if canonical_urls is not None:
    filters.append(Announcement.canonical_url.in_(canonical_urls))
```

不得改變現有 unit、日期、fixture 過濾與三種排序規則。

- [ ] **Step 4: 執行 Task 3 與關聯回歸測試確認 GREEN**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_retrieval.py services\api\tests\test_tools.py services\api\tests\test_keyword_search.py -q
```

Expected: PASS。

- [ ] **Step 5: 執行完整驗證**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests
git diff --check
```

Expected: 全部測試通過，僅 opt-in live smoke 顯示 skipped；`git diff --check` 無錯誤。

- [ ] **Step 6: 執行 opt-in 官網 live smoke**

Run:

```powershell
$env:PYTHONPATH=(Resolve-Path 'services\api\src').Path
$env:RUN_LIVE_KEYWORD_SEARCH='1'
& 'C:\Users\lukes\OneDrive\文件\nptu-campus-assistant\services\api\.venv\Scripts\python.exe' -m pytest services\api\tests\test_live_keyword_search.py -q
```

Expected: PASS，且結果至少一筆。

- [ ] **Step 7: 提交 Task 3**

```powershell
git add services/api/src/nptu_assistant/rag/retrieval.py services/api/tests/test_retrieval.py docs/superpowers/plans/2026-07-13-keyword-announcement-relevance.md
git commit -m "fix(rag): scope keyword answers to official results"
```
