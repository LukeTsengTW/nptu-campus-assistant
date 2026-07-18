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
