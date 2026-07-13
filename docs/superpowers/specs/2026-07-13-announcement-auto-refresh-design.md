# 最新公告自動刷新設計

## 目標

使用者詢問「幫我查最新公告」等最新公告問題時，系統先依官方 Allowlist 來源刷新公告，再從資料庫查詢並回答。背景工作亦依 `crawl_interval_minutes: 60` 定期刷新。任何正式回答仍只引用資料庫已保存的來源 URL。

## 現況

- `data/sources/announcements.yaml` 已登錄國立屏東大學總覽 RSS：`https://www.nptu.edu.tw/p/503-1000-1044.php?Lang=zh-tw`。
- `NptuOverviewAdapter` 已解析 RSS，`CrawlerService` 已取得公告正文。
- `announcements.canonical_url` 已具唯一限制；相同 URL、相同內容回報 `unchanged`，內容改變則更新原列。
- `search_announcements` 已支援 `newest` 與最多 20 筆，但目前只查資料庫。
- `crawl_interval_minutes` 已寫入設定及 `sources` 資料表，目前未控制排程或聊天查詢前刷新。

## 採用方案

採混合刷新：背景定期檢查，加上最新公告查詢前的 freshness gate。

1. 背景排程每 60 秒檢查來源是否到期；實際到期門檻讀取各來源的 `crawl_interval_minutes`，`nptu-overview` 為 60 分鐘。
2. `search_announcements(sort="newest")` 執行前也檢查同一 freshness gate。
3. 未到期直接查資料庫，不發出網路請求。
4. 到期時只允許呼叫設定檔中的 `nptu-overview`，不得接受 LLM 傳入 URL。
5. 官方 RSS 最多處理前 20 則；每則 detail URL 仍須通過現有 NPTU HTTPS Allowlist 與 `robots.txt` 檢查。
6. 完成資料庫交易後，再執行公告查詢，確保回答看到新資料。

此方案比純 request-time 爬取更穩定：一般查詢不必等待；服務閒置時仍會更新。比新增 Celery、Redis 或第三方 scheduler 小，符合目前單一 API container 的 MVP 架構。

## 元件

### `AnnouncementRefreshCoordinator`

新增於 `services/api/src/nptu_assistant/crawlers/refresh.py`。

- 載入 `announcements.yaml`。
- 取得來源最後成功刷新時間。
- 依 `crawl_interval_minutes` 判斷是否到期。
- 使用 process-local lock，避免背景排程與使用者查詢同時爬取。
- 鎖取得後再次判斷 freshness，避免等待期間完成刷新後又重複執行。
- 呼叫既有 `CrawlerService.run([source_name])`。
- 回傳結構化 `RefreshResult`，包含是否執行、是否成功、警告。

最後成功時間取兩者較新值：

- 資料庫該來源公告的 `max(last_crawled_at)`。
- 本程序成功但未取得任何公告時記錄的 in-memory timestamp。

如此不需新增資料庫欄位。服務重啟後若來源沒有公告，允許立即重試一次。

### `AnnouncementRefreshScheduler`

同檔案提供非阻塞背景迴圈：

- 啟動時立即檢查一次。
- 之後每 60 秒檢查所有 `enabled: true` 來源。
- 同步爬蟲透過 `asyncio.to_thread` 執行，不阻塞 FastAPI event loop。
- 應用程式關閉時設定 stop event，等待工作結束。

60 秒是檢查頻率，不是爬取頻率；爬取頻率仍由 `crawl_interval_minutes: 60` 控制。

### 聊天工具整合

`ToolExecutor` 接受可選 refresher。執行 `search_announcements` 且 `sort == newest` 時：

1. 呼叫 `ensure_fresh("nptu-overview")`。
2. 無論刷新成功或失敗，都再查資料庫。
3. 刷新失敗但資料庫有舊資料時，工具回傳穩定警告：`最新公告更新失敗，以下內容來自資料庫最後成功收錄的資料。`
4. `ChatService` 將該警告直接帶入 `ChatResponse.warning`，不依賴 LLM 自行重述。
5. 資料庫也沒有證據時，維持既有「目前收錄的官方資料不足以確認。」行為。

## 去重規則

- 完全相同 `canonical_url` 與內容雜湊：不新增，回報 `unchanged`。
- 相同 `canonical_url`、官方內容已變更：更新原列，回報 `updated`。
- 新 `canonical_url`：新增，回報 `created`。
- 資料庫唯一限制維持最後防線。

## 失敗處理

- RSS 或 detail 失敗：沿用 `CrawlerService` 摘要與既有 detail fallback。
- 整個來源刷新失敗：保留資料庫舊資料，聊天回應附固定警告。
- 排程刷新失敗：記錄錯誤，下個檢查週期依 freshness 再試；不使 API 程序退出。
- 等待鎖的查詢：鎖釋放後重新檢查 freshness；通常直接使用剛完成的資料。
- 不將任意 URL、LLM 生成 URL 或非 NPTU 網域送入爬蟲。

## 設定及資料庫

- 在 `nptu-overview` 明確加入 `max_items: 20`。
- 保留 `crawl_interval_minutes: 60`，由 coordinator 實際使用。
- 不新增 Chrome 權限。
- 不新增秘密金鑰。
- 不蒐集個人資料。
- 本次不變更資料庫 schema，因此不需 Migration。

## 測試

- freshness 未到期：不呼叫爬蟲。
- freshness 到期：呼叫一次指定來源。
- 首次執行：立即爬取。
- 兩個並行觸發：只爬取一次。
- 成功但零筆：60 分鐘內不重複爬取。
- 背景 scheduler：啟動檢查、週期檢查、正常停止。
- `newest` 查詢：先刷新，再查資料庫。
- 非 `newest` 查詢：不做 request-time 刷新。
- 刷新失敗且有舊資料：回答保留 DB 來源並附固定警告。
- 刷新失敗且無資料：回覆資料不足。
- YAML：`max_items == 20`、`crawl_interval_minutes == 60`。
- 既有 crawler、RAG、API、provider 測試全部通過。

## 非目標

- 不允許 LLM 指定爬取 URL。
- 不抓取非 Allowlist 網域。
- 不新增教師評價或個人校務資料。
- 不引入 Celery、Redis、APScheduler。
- 不保證多個 API process 間的分散式鎖；目前 Docker Compose 為單一 API process。未來多 worker 部署時再改用 PostgreSQL advisory lock。

## 資料來源

- 國立屏東大學最新消息總覽：https://www.nptu.edu.tw/p/422-1000-1044.php?Lang=zh-tw
- 國立屏東大學 robots.txt：https://www.nptu.edu.tw/robots.txt
- `data/sources/announcements.yaml`
- `services/api/src/nptu_assistant/crawlers/service.py`
- `services/api/src/nptu_assistant/db/repositories.py`
- `services/api/src/nptu_assistant/rag/tools.py`
- `services/api/src/nptu_assistant/rag/retrieval.py`
- `services/api/src/nptu_assistant/rag/service.py`
