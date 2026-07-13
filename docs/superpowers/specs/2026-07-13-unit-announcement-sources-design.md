# 依單位官方來源查詢最新公告設計

## 背景與目標

目前公告查詢分成兩條路徑：無關鍵字的最新公告會依 `crawl_interval_minutes` 刷新 `nptu-overview` RSS；有關鍵字的公告會使用中央 association search，即時匯入成功取得的 canonical URLs 後再限制資料庫檢索範圍。學術與行政單位名稱則只透過 prompt 與中央搜尋 aliases 處理，尚未具備「單位 → 固定官方來源」的後端路由。

本功能新增可擴充的單位官方 HTML 公告來源。第一個正式來源為資訊學院官網「更多最新公告」靜態列表 `https://ccs.nptu.edu.tw/p/403-1025-1019-1.php?Lang=zh-tw`。當問題同時包含單位與公告意圖時，後端以 deterministic resolver 決定來源，模型不得提供 URL、host、selector 或 source name。首頁的最新公告由 JavaScript 動態載入，因此 crawler 使用首頁直接連結的同站靜態列表。

## 範圍

- 支援 `sources[].aliases`、`allowed_hosts`、CSS selectors 與可選 detail 行為。
- 新增通用、設定驅動的 NPTU HTML list adapter。
- 新增 deterministic unit source resolver，區分 resolved、unknown、ambiguous、unsupported 與未指定單位。
- 單位來源刷新後只用該來源最後成功的 canonical URL 快照檢索。
- 維持 `nptu-overview` RSS、中央關鍵字搜尋、一般文件搜尋、公開 API schema 與 Extension 權限不變。
- 更新文件、fixture、單元測試、整合測試與 opt-in live smoke test。

本次不新增教師評價、個人校務資料、Chrome 權限、秘密金鑰或使用者資料收集。

## 方案選擇

採用「YAML 驅動通用 selector adapter + deterministic resolver + 持久來源快照」。相較每個網站硬編碼 adapter，此方案新增來源時只需設定、fixture 與測試；相較沿用中央搜尋，它能證明結果來自指定單位官網；相較由 LLM 傳 URL，來源與 SSRF 邊界完全由後端控制。

## 設定模型

`CrawlerSourceConfig` 禁止未知欄位，並新增：

- `aliases: list[str]`
- `allowed_hosts: list[str]`
- `selectors: HtmlListingSelectors | None`
- `detail: DetailPageConfig | None`

`HtmlListingSelectors` 包含 `listing`、`item`、`date`、`title_link` 與預設為 `href` 的 `link_attribute`。所有 selector 在載入設定時以 SoupSieve 編譯驗證；空 selector、空 alias、重複 alias、非 NPTU host、來源 URL host 不在 `allowed_hosts`、HTML adapter 缺 selectors 都直接拒絕。

`allowed_hosts` 使用網域邊界比對：`ccs.nptu.edu.tw` 只接受該 host 及其子網域；`nptu.edu.tw` 可供既有 overview 使用並涵蓋全部官方子網域。所有 URL 仍須通過全域 HTTPS NPTU allowlist。

資訊學院設定：

```yaml
- name: information-college-html
  adapter: nptu_html_list
  url: https://ccs.nptu.edu.tw/p/403-1025-1019-1.php?Lang=zh-tw
  unit: 資訊學院
  aliases:
    - 資訊學院
  category: 學術單位公告
  enabled: true
  crawl_interval_minutes: 60
  max_items: 20
  allowed_hosts:
    - ccs.nptu.edu.tw
  selectors:
    listing: section.mb
    item: .row.listBS
    date: i.mdate
    title_link: .mtitle > a[href]
    link_attribute: href
  detail:
    enabled: false
```

## Alias 與單位解析

既有 `keyword_search.aliases` 繼續作為已知單位別名 catalog。共用 `AliasNormalizer` 負責最長別名優先的 deterministic normalization；中央關鍵字搜尋與來源 resolver 都重用此實作。

`UnitSourceResolver` 同時讀取：

1. `sources[].unit` 與 `sources[].aliases`：可路由的來源。
2. `keyword_search.aliases` 的 alias 與 canonical value：已知但可能尚未有官方來源的單位。

解析狀態：

- `resolved`：唯一對應 enabled source；回傳 canonical unit 與 source name。
- `ambiguous`：同一別名、多個來源或問題同時包含多個單位；不搜尋並要求澄清。
- `unsupported`：可由既有 catalog 辨識，但沒有 enabled source；明確回覆目前未支援。
- `unknown`：有明確 unit 參數或公告問題中出現未知單位型名稱；不查網路、不做模糊 DB fallback。
- `none`：未指定單位；保留既有 overview／中央關鍵字搜尋流程。

若 tool 的 `unit` 與 query 指向不同單位，狀態為 `ambiguous`。來源 resolver 不接受 URL，也不把使用者字串轉成網址。

## HTML adapter 與安全邊界

`NptuHtmlListAdapter` 只在 `listing` roots 內尋找 `item`，每筆只讀設定指定的 date 與 title/link selector：

1. 清理日期與標題空白。
2. 以既有 `parse_published_at` 支援 ISO、西元與民國年。
3. 以 `urljoin` 解析相對 URL。
4. canonicalization 會小寫 host、移除 fragment，並拒絕 userinfo、非 443 port、非 HTTPS、非 NPTU 或不在來源 `allowed_hosts` 的 URL。
5. 日期、標題或 href 缺失、日期非法、host 非法的單筆資料會記錄結構化 warning 並略過，不使其他有效項目失敗。
6. 以 canonical URL 去重；依日期降冪 stable sort，故同日維持頁面順序。
7. listing root、item 或有效公告為零時視為來源結構失敗，避免把 selector 漂移誤記為成功刷新。

detail 抓取由設定控制。啟用時仍為 best effort；失敗會保留 listing 的日期、標題與 URL，並把固定 warning 存入公告。HTML 內容永遠只當不可信資料清理，不會成為系統指令。

`CrawlHttpClient` 改為不自動跟隨 redirect。每個 redirect target 在發出下一個 request 前重新驗證全域與來源 host allowlist，從而阻止外站、localhost、私有 IP literal 或任意使用者 URL 的 SSRF。保留 robots、5/15 秒 timeout、三次有限重試、每 host request interval，並增加 2 MiB response size 上限與最多五次 redirect。

## Crawl、資料庫與 refresh scope

`CrawlerService.run()` 的公開回傳仍是 `CrawlSummary`。新增內部 `run_with_urls()` 回傳 summary 與成功處理、已去重且排序完成的 canonical URL tuple。

`sources` 表新增：

- `last_successful_crawl_at timestamptz null`
- `canonical_urls jsonb not null default []`

舊 schema 沒有 crawl-run 邊界，無法證明既有 `last_crawled_at` 來自完整成功的 crawl，因此 Migration 只新增欄位，不偽造成功快照；升級後由 freshness gate 立即執行真實刷新。這個來源快照解決公告 canonical URL 全域唯一造成的 provenance 缺口：同一公告可維持單一資料列，但每個 crawl source 仍保存自己最後成功看到的 URL 範圍。

成功 refresh（含成功空結果）以單一交易提交公告 upsert、完整來源快照與成功時間；排程、查詢前刷新與手動 crawl 都共用此 repository contract。任一步驟失敗會整批回滾且不覆寫舊快照。`RefreshResult` 新增 `canonical_urls`：

- 非空 tuple：只查這些 URL。
- 空 tuple：來源成功但沒有結果，直接回資料不足。
- freshness no-op：讀取持久快照。
- refresh 失敗：讀取最後成功快照並附 `REFRESH_FAILURE_WARNING`。
- 無任何快照：不得做全校 broad fallback，回資料不足與 warning。

## ToolExecutor 與檢索

公開 `search_announcements` tool schema 不新增 source 或 URL 欄位。ToolExecutor 在 keyword ingestor 之前解析 unit：

- `resolved`：刷新 resolver 選定的 source；使用 canonical unit 與 refresh snapshot URLs；不呼叫中央 keyword ingestor，也不刷新 `nptu-overview`。
- `unknown`／`ambiguous`／`unsupported`：回傳穩定的 structured error，不呼叫 crawler、retriever 或任意外部 URL。
- `none`：維持既有中央搜尋與 overview refresh。

來源 query 的 `relevance` 預設改為 `newest`；明確 `oldest` 仍尊重。limit 仍由 Pydantic 限制 1 至 20。

`SqlRetriever` 在 canonical URL scope 非空時先取得完整 scope（最多來源 `max_items`），再以日期降冪及 URL tuple index stable sort，最後套 limit，避免同日邊界被 DB `last_crawled_at` 顛倒。一般查詢排序維持原行為。

## Prompt 與回覆

Prompt 明定：

- 單位名稱與「最新公告、最近公告、公告、最新消息、消息」同時出現時使用 `search_announcements`。
- 單位介紹、業務、規章或一般文件才使用 `search_documents`。
- resolver error 必須照 structured message 澄清或說明未支援，不得猜測網址。
- 公告回答逐筆列出日期、標題與工具 URL；預設五筆、最多二十筆。
- 資料來源標示為 canonical unit 官方網站；只能使用 tool evidence URL 與 source IDs，且不得顯示 UUID。

既有 sanitizer 與 `SourceReference` 官方 URL 驗證維持最後一道防線。refresh warning 由 ToolExecutor 固定常數傳至 `ChatResponse.warning`，模型不能自行猜測 refresh 狀態。

## 相容性

- `/v1/chat`、`/v1/announcements`、OpenAI tool schema 與 OpenAPI 不變。
- 既有 RSS、fixture、中央關鍵字搜尋與 document search 保留。
- `RefreshResult` 只在尾端新增有預設值欄位，既有呼叫仍相容。
- `CrawlHttpClient.get()` 只新增 keyword-only optional allowlist；既有呼叫不變。
- 新增 Alembic Migration，未直接修改既有 Migration。
- Extension 不新增權限，仍只連本機 API。

## 測試與驗收

- 設定：完整資訊學院設定、未知欄位、URL/host、source name、alias、selector 驗證。
- Adapter：六筆 fixture、日期與 URL、stable sort、相對 URL、空白、缺欄位、非法 host、重複 URL、其他 section。
- HTTP：redirect 發出前阻擋、robots、response size、timeout/retry 回歸。
- Resolver：resolved、unknown、ambiguous、unsupported、query/unit conflict、未指定單位。
- Repository/Migration：不偽造舊成功快照、公告與快照同交易成功更新、錯誤整批回滾、URL 重疊。
- Refresh/Tool/Retrieval：來源選擇、URL scope、no-op、失敗舊資料、空結果、同日順序、既有 overview/keyword 回歸。
- Chat/provider：prompt 路由、固定 warning、來源與 URL sanitization、繁中格式。
- Live smoke：只在 `RUN_LIVE_UNIT_ANNOUNCEMENT_SOURCE=1` 時執行，不讓 CI 依賴外站。
- 完整 API tests、Windows tests、Extension typecheck/test/build、Migration 與 `git diff --check`。

## 已知限制

- 第一版只有資訊學院正式來源；研發處、計算機與網路中心、各系所等即使能辨識，也會明確回覆尚未支援其公告來源。
- CSS selector 隨官網改版需要更新 YAML、fixture 與對應測試。
- detail 頁預設不抓取；列表欄位足以提供最新公告日期、標題與官方 URL。
