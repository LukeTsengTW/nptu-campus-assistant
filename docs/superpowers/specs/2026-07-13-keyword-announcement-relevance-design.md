# 關鍵字公告相關性範圍設計

## 問題

非空公告查詢會先搜尋國立屏東大學官網並將結果收錄至資料庫，但目前 DB 檢索只計算文字相似度，不以相似度限制候選集合。當工具要求 `newest` 時，系統會直接從所有公告中選取日期最新的項目，導致分數接近零的無關公告取代官網剛找到的相關公告。

## 目標

- 非空 query 且官網搜尋至少部分成功時，只從本次官網搜尋所得 canonical URL 對應的 DB 公告產生 Evidence。
- `newest`、`oldest`、`relevance` 只決定相關候選集合內的排序。
- 保留「相關公告可以由其他單位發布」的行為，不推導或強制單位篩選。
- 正式回答的來源 URL 仍只來自 DB Evidence。
- 不修改公開 API、工具 JSON schema、Extension、Chrome 權限或資料庫 schema。

## 資料流

1. `KeywordAnnouncementSearchService.ingest(query)` 依既有流程展開別名，提交 `part` 與 `com` 搜尋，合併並以 canonical URL 去重。
2. 服務在收錄每一筆候選公告後，將本次有效的 canonical URL 納入 `KeywordIngestionResult`。URL 只作內部 DB 範圍條件，不直接交給回答模型。
3. `ToolExecutor` 在官網搜尋有可用 URL 時，將 canonical URL 集合傳給 `SqlRetriever.search_announcements`。
4. `SqlRetriever` 先以 `Announcement.canonical_url IN (...)` 限定候選，再套用既有日期、單位條件與排序。
5. Retriever 仍從 DB 建立 Evidence；LLM 不自行產生來源 URL。

## 失敗行為

- 部分搜尋失敗且仍取得 URL：只檢索成功取得的 URL，並保留部分更新失敗警告。
- 搜尋全部失敗：不設定 URL 範圍，沿用完整系名 query 查詢最後成功收錄的 DB 資料，並附官網搜尋失敗警告。
- 搜尋成功但沒有結果：設定空 URL 範圍，DB 回傳空 Evidence，正式回答明確表示資料不足；不得退回全校最新公告。
- 官網結果已存在 DB：canonical URL 仍納入範圍，無論 upsert 結果是 `created`、`updated` 或 `unchanged`。

## 內部介面

- `KeywordIngestionResult` 新增不可變的 `canonical_urls: tuple[str, ...]`。
- `StructuredRetriever.search_announcements` 與 `SqlRetriever.search_announcements` 新增內部參數 `canonical_urls: tuple[str, ...] | None`。
  - `None`：沒有可靠的本次搜尋範圍，使用既有 DB fallback。
  - 空 tuple：官網搜尋成功但沒有結果，必須回傳空集合。
  - 非空 tuple：只查詢指定 canonical URL。
- 公開工具 schema 保持不變，canonical URL 範圍不能由 LLM 傳入。

## 測試

- 搜尋服務回傳去重後 canonical URL，並涵蓋 `created`、`updated`、`unchanged`。
- 部分失敗保留成功 URL；全部失敗回傳 `canonical_urls=None`；成功但零結果回傳空 tuple。
- ToolExecutor 將內部 URL 範圍傳給 retriever，且不改公開工具參數。
- Retriever 回歸測試建立「較新的無關公告」與「較舊的相關公告」，確認 `newest` 只在 URL 範圍內排序。
- 空 URL 範圍回傳零筆，不退回全校公告。
- 完整 API 測試與 opt-in live smoke 維持通過。

## 非目標

- 不新增嚴格的「電腦科學與人工智慧學系」單位限制。
- 不調整 LLM 對 `newest` 或 `relevance` 的選擇策略。
- 不新增相似度門檻或資料庫 Migration。
