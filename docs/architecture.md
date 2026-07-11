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
