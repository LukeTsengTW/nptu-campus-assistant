# 依單位官方來源查詢最新公告 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 讓「資訊學院最新公告」固定刷新資訊學院官方 HTML 來源並以該來源 canonical URLs 回傳最新五筆，同時保留既有 RSS、中央搜尋與文件查詢。

**Architecture:** 以 typed YAML config 建立通用 HTML adapter 與 deterministic unit resolver。Crawler 將最後成功 URL 範圍持久化在 `sources`，ToolExecutor 先解析單位再選 source，SqlRetriever 僅在該 URL scope 內排序與限制。

**Tech Stack:** Python 3.12、Pydantic、BeautifulSoup/SoupSieve、httpx、SQLAlchemy、Alembic、PostgreSQL/JSONB、pytest。

## Global Constraints

- 所有使用者可見文字使用繁體中文，且正式回答必須有資料庫 Evidence 來源。
- URL 只能來自設定與資料庫，不可由 LLM 或使用者提供。
- 公開 API schema、Extension 權限、既有 RSS、中央關鍵字搜尋與 document search 必須相容。
- 每個 production behavior 必須先有會因缺少功能而失敗的測試。
- live smoke 預設 skip；完整 CI 不依賴外部網站。

---

### Task 1: Typed source config、URL 安全與 fixture

**Files:**
- Modify: `services/api/src/nptu_assistant/core/security.py`
- Modify: `services/api/src/nptu_assistant/crawlers/config.py`
- Modify: `data/sources/announcements.yaml`
- Create: `data/fixtures/announcements/nptu-ccs/listing.html`
- Create: `services/api/tests/test_unit_source_config.py`
- Modify: `services/api/tests/test_core.py`

**Interfaces:**
- Produces: `HtmlListingSelectors`, `DetailPageConfig`, expanded `CrawlerSourceConfig`, `is_allowed_source_url(url, allowed_hosts)`, `canonicalize_nptu_url(url)`.

- [x] Write failing config/security tests for CCS fields, invalid URL/host/source name/alias/selectors and canonical URL normalization.
- [x] Run targeted tests and confirm missing fields/functions fail.
- [x] Implement strict Pydantic models and source-host validation.
- [x] Add `information-college-html` config and six-row fixture using the supplied dates/URLs.
- [x] Re-run targeted tests and confirm green.

### Task 2: Configurable HTML announcement adapter

**Files:**
- Create: `services/api/src/nptu_assistant/crawlers/adapters/nptu_html.py`
- Create: `services/api/src/nptu_assistant/crawlers/adapters/factory.py`
- Modify: `services/api/src/nptu_assistant/crawlers/service.py`
- Create: `services/api/tests/test_configurable_announcement_adapter.py`
- Modify: `services/api/tests/test_services.py`

**Interfaces:**
- Consumes: `CrawlerSourceConfig`, `HtmlListingSelectors`, URL security helpers.
- Produces: `NptuHtmlListAdapter.parse_listing(content) -> list[AnnouncementCandidate]`, `build_adapter(config)`.

- [x] Write failing tests for six parsed rows, stable date sorting, relative URLs, invalid/missing items, duplicate URLs, unrelated sections and selector drift.
- [x] Run the adapter tests and verify they fail because the adapter does not exist.
- [x] Implement the minimal selector-only parser, structured diagnostics and adapter factory.
- [x] Replace CrawlerService adapter if/else with the factory while preserving fixture and overview behavior.
- [x] Re-run adapter/service/crawler tests.

### Task 3: Redirect-safe HTTP policy

**Files:**
- Modify: `services/api/src/nptu_assistant/crawlers/http.py`
- Modify: `services/api/tests/test_services.py`

**Interfaces:**
- Produces: `CrawlHttpClient.get(url, *, allowed_hosts=None)`, pre-request redirect validation, 2 MiB size limit, five redirect cap.

- [x] Add failing tests proving an external or wrong-NPTU-host redirect target is never requested, oversized responses fail, and allowed redirects retain robots/retry behavior.
- [x] Run tests and confirm the current auto-follow implementation fails the no-request assertion.
- [x] Implement manual redirect handling and response size validation.
- [x] Re-run HTTP and keyword-search regressions.

### Task 4: Shared aliases and deterministic unit source resolver

**Files:**
- Create: `services/api/src/nptu_assistant/crawlers/aliases.py`
- Create: `services/api/src/nptu_assistant/crawlers/resolution.py`
- Modify: `services/api/src/nptu_assistant/crawlers/search.py`
- Create: `services/api/tests/test_unit_source_resolution.py`
- Modify: `services/api/tests/test_keyword_search.py`

**Interfaces:**
- Produces: `AliasNormalizer`, `UnitResolutionStatus`, `UnitResolution`, `UnitSourceResolver.resolve(unit, query)`.

- [x] Add failing tests for resolved canonical/alias, unsupported known alias, unknown suffix, duplicate alias ambiguity, multi-unit query, query/unit conflict and no-unit state.
- [x] Run tests and verify resolver imports fail.
- [x] Extract longest-first normalization into `AliasNormalizer`; retain `KeywordAliasResolver.expand()` as a thin subclass.
- [x] Implement source matching with deterministic, sorted ambiguity candidates.
- [x] Re-run resolver and all keyword alias tests.

### Task 5: Persist source refresh snapshots with Migration

**Files:**
- Modify: `services/api/src/nptu_assistant/db/models.py`
- Modify: `services/api/src/nptu_assistant/db/repositories.py`
- Create: `database/migrations/versions/20260713_0003_source_refresh_snapshots.py`
- Modify: `services/api/tests/test_models.py`
- Modify: `services/api/tests/test_services.py`
- Modify: `tests/integration/test_postgres_flow.py`

**Interfaces:**
- Produces: `Source.last_successful_crawl_at`, `Source.canonical_urls`, `SqlAnnouncementRepository.record_source_refresh(...)`, `canonical_urls_for_source(source_name)`.

- [x] Add failing model/repository tests for snapshot save/load and metadata-only upsert updates.
- [x] Add Migration test expectations that legacy rows remain unverified until a real refresh.
- [x] Run targeted tests and confirm columns/methods are missing.
- [x] Implement model columns, repository transaction methods and Alembic upgrade/downgrade without fabricating a legacy success snapshot.
- [x] Run unit and PostgreSQL integration tests; run upgrade twice to verify idempotent head state.

### Task 6: Canonical URLs through crawler and refresh

**Files:**
- Modify: `services/api/src/nptu_assistant/crawlers/models.py`
- Modify: `services/api/src/nptu_assistant/crawlers/service.py`
- Modify: `services/api/src/nptu_assistant/crawlers/refresh.py`
- Modify: `services/api/tests/test_refresh.py`
- Modify: `services/api/tests/test_services.py`

**Interfaces:**
- Produces: `CrawlRunResult(summary, canonical_urls)`, `CrawlerService.run_with_urls()`, `RefreshResult.canonical_urls`.

- [x] Add failing tests for successful URL tuple, successful empty tuple, freshness no-op persistent snapshot and refresh failure retaining the prior tuple/warning.
- [x] Run tests and confirm current result objects lack URLs.
- [x] Implement `run_with_urls()` while keeping `run()` public behavior unchanged.
- [x] Update coordinator to record only successful snapshots and read persisted scope on no-op/failure.
- [x] Re-run refresh, scheduler, CLI and API lifespan tests.

### Task 7: Tool routing and stable scoped retrieval

**Files:**
- Modify: `services/api/src/nptu_assistant/rag/tools.py`
- Modify: `services/api/src/nptu_assistant/rag/retrieval.py`
- Modify: `services/api/src/nptu_assistant/wiring.py`
- Modify: `services/api/tests/test_tools.py`
- Modify: `services/api/tests/test_retrieval.py`
- Modify: `services/api/tests/test_api.py`

**Interfaces:**
- Consumes: `UnitSourceResolver`, `RefreshResult.canonical_urls`.
- Produces: stable `unknown_unit`, `ambiguous_unit`, `unsupported_unit_source` tool errors; resolved-source refresh before retrieval.

- [x] Add failing ToolExecutor tests proving source resolver runs before keyword ingestor, CCS source is selected, errors cause zero I/O, and general searches retain old behavior.
- [x] Add failing retrieval test for same-date order following canonical URL tuple and limit-after-scope.
- [x] Run tests and confirm current hard-coded overview/keyword paths fail.
- [x] Implement optional resolver dependency, source-specific refresh and safe no-scope behavior without changing tool schema.
- [x] Reorder scoped Evidence in SqlRetriever and wire one shared config/resolver instance.
- [x] Re-run tools/retrieval/wiring/API contract tests.

### Task 8: Prompt、fake provider、chat warning 與格式

**Files:**
- Modify: `services/api/src/nptu_assistant/rag/prompts.py`
- Modify: `services/api/src/nptu_assistant/providers/fake.py`
- Modify: `services/api/tests/test_rag.py`
- Modify: `services/api/tests/test_providers.py`
- Modify: `services/api/tests/test_chat_orchestration.py`

**Interfaces:**
- Produces: announcement-intent precedence, five-row Traditional Chinese official-site answer in fake mode, resolver error clarification behavior.

- [x] Add failing prompt tests for unit+announcement routing and document-only routing.
- [x] Add failing fake/chat tests for five rows with date/title/URL, no UUID, supported/unsupported/ambiguous responses and refresh warning.
- [x] Update prompt and fake provider minimally; keep OpenAI strict schemas unchanged.
- [x] Re-run chat orchestration, provider and sanitizer tests.

### Task 9: Docs、live smoke 與完整驗收

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/data-sources.md`
- Modify: `docs/testing.md`
- Modify: `services/api/README.md`
- Create: `services/api/tests/test_live_unit_announcement_source.py`
- Modify: this plan with verification results.

- [x] Document add-source workflow, aliases, selectors, allowlist/robots/timeout/retry/refresh, fixture maintenance and first-source limitation.
- [x] Add opt-in live smoke controlled by `RUN_LIVE_UNIT_ANNOUNCEMENT_SOURCE=1`.
- [x] Run targeted tests, full API tests, Windows tests, Extension typecheck/test/build, migration/integration checks and `git diff --check`.
- [x] Verify the acceptance question resolves to `information-college-html`, newest, limit five, with only CCS URLs.
- [x] Record exact commands/results and remaining unsupported units in the implementation report.

## 驗收結果

- API：`244 passed, 2 skipped`；兩個 skip 都是預設不連外的 opt-in live tests。
- PostgreSQL：Migration head/current 均為 `20260713_0003`；整合測試 `7 passed`，包含公告與來源快照同交易回滾。
- 資訊學院 live smoke：設定 `RUN_LIVE_UNIT_ANNOUNCEMENT_SOURCE=1` 後 `1 passed`；官方靜態列表、robots 與 selectors 均符合契約。
- Windows 啟動腳本：`3 passed`。
- Extension：typecheck 通過、`11 passed`、production build 通過；未新增 Chrome 權限。
- 品質檢查：`git diff --check` 與 Python compileall 通過。
- 目前只正式支援資訊學院單位公告來源；可辨識但未設定來源的其他學術／行政單位會明確回覆尚未支援，未知或多重單位則要求釐清。
