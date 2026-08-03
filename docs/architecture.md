# 系統架構

## 元件

1. WXT content script 在 NPTU 網域掛載 Shadow DOM React UI。
2. MV3 service worker 接收 typed message，透過明確 API client 呼叫 FastAPI。
3. FastAPI 模組化單體協調資料庫、crawler、ingestion、retrieval 與 providers。
4. PostgreSQL 保存官方來源、文件版本、chunks、向量與公告。
5. OpenAI/Fake providers 實作相同 protocol，測試不依賴外部 API。

## 邊界

- Extension 不讀取原始頁面內容，也不接觸資料庫或 OpenAI。
- 管理端點只能觸發固定本機資料目錄或設定檔來源。
- Provider 不負責 URL；正式來源永遠由 repository 查回。
- Crawler candidate 必須經 URL allowlist、HTML 清理與 schema 驗證後才能保存。

## 執行模型

MVP 採單一 API process。文件匯入與公告爬取可由 CLI 或同步管理端點執行；不導入 queue。若未來工作量需要，再以既有 service protocol 外移 worker。

## 單位公告查詢資料流

```text
使用者問題
  → search_announcements
  → official_units.yaml（正式名稱、alias、homepage、host、seed、公告能力）
  → UnitSourceResolver（辨識單位，不把辨識結果等同 dedicated listing）
  → configured_listing：指定來源 freshness gate／canonical URL snapshot
    或 scoped_site_search：只從單位 seed 與 allowed host 探索公告頁
    或同單位可信 cache
  → Evidence
  → URL／內部 ID sanitizer
  → ChatResponse.sources
```

`data/sources/official_units.yaml` 是學術單位唯一 registry，涵蓋 66 筆官方清單資料：64 個 active、2 個 discontinued。alias、homepage、allowed host、site-search seeds 與公告策略由 typed loader 一次驗證。`configured_listing` 會生成 crawler source；`scoped_site_search` 不因缺少固定 selector 變成 unsupported；`unsupported` 必須附原因。模型不能提供 URL、host、selector 或 source name。

公告意圖優先於單位介紹意圖。共用 classifier 區分 homepage、announcement、document；「最新資訊」是公告意圖，「資訊安全課程規定」仍是文件意圖。移除單位、操作詞、數量與 latest 詞後，空主題使用 `query=null`；剩餘文字保留為真正 topic。

來源快照以 `Source.last_successful_crawl_at` 與 `Source.canonical_urls` 表示三種狀態：尚未成功為 `NULL`、成功但零筆為空陣列、成功且有資料為 URL 陣列。公告 upsert、完整 URL 快照與成功時間在同一 repository transaction 提交，排程、查詢前刷新與手動 crawl 共用此流程。來源中途失敗會整批回滾，不得推進成功時間或覆寫快照。

單位解析狀態分為 `unknown_unit`、`known_unit_with_listing`、`known_unit_with_scoped_search`、`known_unit_without_verified_site` 與 `ambiguous_unit`。單位 scoped 搜尋失敗只允許回退同單位 cache；不得用全校或其他單位結果補空缺。

homepage intent 直接由 registry 建立 deterministic config-backed official evidence，固定第一名、warning 為 `None`，不依賴即時 crawl 判定 URL 是否存在。一般文件檢索則將 `DocumentSearchScope` 同時傳入初始 DB retrieval、unit-seeded live search、ingestion 與 refreshed retrieval；全流程共用同一 `SearchDeadline`。refreshed retrieval 完成但空集合、最後回退 weak cache 時，明確回傳 partial warning。

## 一般官方網頁搜尋資料流

```text
使用者問題與最近對話
  → 同一個 LLM tool-calling 回合產生嚴格 SearchPlan
  → 先查既有 documents／document_chunks
  → 高可信且內容足夠：直接回傳
  → 否則以官方搜尋表單、設定 seed 取得候選 URL
  → bounded best-first crawl（anchor／URL／parent relevance）
  → title／heading／body／中文 n-gram／批次 embedding hybrid scoring
  → canonical URL 與 content hash 去重
  → 寫入 official_web_page、chunks 與 embeddings
  → 重新執行 vector search、keyword search 與 RRF
  → Evidence → LLM 回答
```

`SearchPlan` 的 `query` 是依對話解除指涉後的獨立問題，`search_queries` 最多 4 個，`concepts` 最多 8 個。這些概念只用於相關性評分，不是全部必須逐字命中的 AND 條件。官方搜尋只提供候選 URL；候選仍須經後端 NPTU allowlist、robots、redirect、HTML content type 與 canonical URL 驗證，模型不能指定 URL。

搜尋診斷將高相關成功、高相關失敗與無關失敗分開。只有高排名候選失敗且可能影響答案時才附 partial warning；正常零結果不視為網路錯誤，已有高可信資料庫 evidence 時也不執行 live crawl。

## P4 DB-first completeness policy

`DbFirstCompletenessPolicy` 是純函式：它只接收 retrieval evidence 的 current/superseded 狀態、scope、SitePage 的最後成功抓取時間、hash/ingestion 狀態、有效 lease、公告 source coverage 與固定的 request deadline，輸出固定的 action 和 reason code。它不做 HTTP、embedding、寫入或讀取時間全域狀態。`SqlRetrievalCompletenessFacts` 對已取得的 canonical URLs 以至多兩條 set-based SQL 查詢取得 metadata；PostgreSQL 在每條 statement 前都重新以 request 剩餘時間套用 transaction-local timeout。公告 evidence 同時必須位於 source 最近一次成功的 canonical snapshot，並以 `Announcement.last_crawled_at` 判斷 detail freshness；沒有對應 `SitePage` 時不會假裝 hash 已同步，`Announcement.warning` 一律視為 retryable incomplete。資料庫錯誤採保守 incomplete decision，不能把錯誤視為完整。

```text
DB retrieval
  → completeness facts
  → fresh/current/exact evidence: USE_DB
  → stale but usable / pending / failed: USE_DB_AND_SCHEDULE_REFRESH
  → active worker or terminal undated announcement: USE_DB_WITH_INCOMPLETE_WARNING
  → genuinely insufficient: USE_BOUNDED_LIVE_FALLBACK
```

`CompletenessRefreshScheduler` 對 SitePage 呼叫既有 `CrawlScheduler.schedule_pages()` 的單條 conditional `UPDATE`，並以 Source watermark 的單條 conditional `UPDATE` 為沒有 SitePage 的公告來源寫入 durable due signal；兩者都不啟動 worker、不等待 crawl。SitePage 排程不推遲較早的 `next_crawl_at`，也不覆寫 blocked、excluded 或持有 live crawl/ingestion lease 的 target；Source worker 在 HTTP 前取得 PostgreSQL session advisory lease，完成後以 crawl-start watermark 的單調檢查提交 snapshot，避免跨程序重複 fetch 與晚到舊快照覆寫。它們同樣受 shared deadline 的 transaction-local timeout 保護；零筆更新不會宣稱 background refresh 已排程。refresh target 上限獨立於 live candidate 上限，預設為 220；超限會明確失敗而非只排前一部分。

P4 的 `enforce` 預設在 `announcements.yaml`，`shadow` 只記錄 policy 與 legacy 是否會 live 的差異，`off` 完整保留 legacy 流程。結構化事件不記錄原始 query、HTML、embedding 或憑證，只包含 query kind/intent、scope、decision/reason、evidence/freshness/coverage/lease 計數、deadline policy latency，以及 fallback/schedule outcome。所有 live path 繼續共用原始 absolute `SearchDeadline`；P4 只會 cap，絕不建立第二個完整 deadline。

文件 fallback 最多 6 個頁面、16 個 candidates、depth 1、8 秒。公告 scoped fallback 使用同一 deadline 與 limits，detail 最多 4 筆；generic/keyword announcement 的 P4 fallback 經 deadline-aware official form client 與同樣受限的 site search，再重新從資料庫查回已持久化 evidence。terminal undated announcement 是 `incomplete` terminal state，只顯示 incomplete warning，不會因此同步重抓。

## P3.1.1 frontier persistence statement benchmark

`tests/integration/test_frontier_persistence_acceptance.py` 是 opt-in 的真實 PostgreSQL acceptance benchmark，不使用 SQLite 代替 PostgreSQL。它把一次 fetched page persistence 視為一個 transaction，並以 SQLAlchemy engine trace 實測 `transaction_count`、實際 `statement_count`、commit／rollback、執行時間、建立的 targets 與 edges；repository 回傳的 `statement_count` 不能取代這份 SQL trace。

驗收 workload 包含同一 host 的 100 個 links、分布於 10 與 100 個 hosts 的 100 個 links、canonical URL 重複、既有／新 target、非 HTML resource、global／per-host pending cap、兩個相反 host 順序的並行 writer，以及故意觸發資料庫長度錯誤的 rollback。測試也驗證 advisory lock 的 host key 以排序後順序取得、兩個 writer 不 deadlock，並確認 duplicate 不會增加 page 或 edge。

P3.1.1 的 statement acceptance 是 workload scaling contract：同 host 的 link 數由 1 增至 100 時，實際 SQL statement 數只能維持固定差距；host 數增加到 100 時不得形成每個 host 一個 lock／COUNT 的無界 N+1。global／per-host cap 必須同時限制 pending targets，非 HTML 只能留下 excluded metadata／edge，rollback 則不得留下 source、target 或 edge。未設定 `RUN_POSTGRES_INTEGRATION=1` 或 `DATABASE_URL` 時，這些測試會 skip；skip 不代表 acceptance 已通過。

本機執行方式：

```powershell
$env:RUN_POSTGRES_INTEGRATION="1"
$env:DATABASE_URL="postgresql+psycopg://<local-user>:<local-password>@127.0.0.1:5432/<local-db>"
cd services/api
uv run pytest ../../tests/integration/test_frontier_persistence_acceptance.py --import-mode=importlib -q -rP
```

若 100-host case 超過固定 statement budget，或 trace 顯示 statement 數隨 host 線性增加，應優先把 frontier selection／pending counts 合併成單一 set-based CTE（以 `VALUES` 或 unnest 載入 normalized targets，按 host 做 windowed cap，再一次回傳 existing／pending counts），並把 per-host advisory lock 改成一個 sorted set-based call，例如：

```sql
WITH ordered_keys AS MATERIALIZED (
  SELECT hashtext(host)::bigint AS lock_key
  FROM unnest(:hosts::text[]) AS incoming(host)
  ORDER BY host
)
SELECT pg_advisory_xact_lock(lock_key)
FROM ordered_keys;
```

目前 production 實作以單一 lock query 依 global、排序後 host 取得 advisory lock，再以一個 set-based state query 同時回傳 existing URL、host pending count 與 global pending count。非 HTML rows 可保留 edge／excluded metadata，但不得進入 HTML pending rank。這是固定 statement budget 的實作契約，不得只調高測試 assertion。
