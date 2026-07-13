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
  → UnitSourceResolver（單位別名與來源設定）
  → 指定來源 freshness gate／crawler
  → Source canonical URL 成功快照
  → 只在快照範圍內查詢 Announcement
  → Evidence
  → URL／內部 ID sanitizer
  → ChatResponse.sources
```

公告意圖優先於單位介紹意圖。「資訊學院最新公告」走單位公告來源；「資訊學院介紹」才走官方文件檢索。`UnitSourceResolver` 只接受啟動時載入的設定，不接受模型提供 URL、host、selector 或內部來源名稱。

來源快照以 `Source.last_successful_crawl_at` 與 `Source.canonical_urls` 表示三種狀態：尚未成功為 `NULL`、成功但零筆為空陣列、成功且有資料為 URL 陣列。公告 upsert、完整 URL 快照與成功時間在同一 repository transaction 提交，排程、查詢前刷新與手動 crawl 共用此流程。來源中途失敗會整批回滾，不得推進成功時間或覆寫快照。

單位解析為未知、歧義或尚未支援時，在網路與資料庫 I/O 前回傳結構化錯誤。已解析來源即使刷新失敗且沒有舊快照，也以空 scope 回傳資料不足，不會退回全校或相似度搜尋。
