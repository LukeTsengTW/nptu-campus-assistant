# Windows 自動啟動設計

## 目標

Windows 使用者登入後，自動等待 Docker Desktop 引擎就緒，啟動本專案的 PostgreSQL 與 API，並確認 `http://127.0.0.1:8000/health` 可連線。

## 設計

- `docker-compose.yml` 的 `db`、`api` 使用 `restart: unless-stopped`。Docker 引擎重新啟動後，既有容器自動恢復。
- `scripts/start-nptu-assistant.ps1` 負責等待 Docker、執行 `docker compose up -d`、等待 API health check。等待採條件輪詢，不使用固定延遲。
- `scripts/install-windows-autostart.ps1` 為目前 Windows 使用者建立登入觸發的排程工作。工作以隱藏 PowerShell 執行啟動腳本，不需要管理員權限。
- 啟動腳本寫入本機暫存目錄日誌；不記錄秘密或使用者資料。
- 安裝腳本可重複執行；相同工作名稱會更新。

## 失敗處理

- Docker 未在期限內就緒：記錄錯誤並回傳非零結束碼。
- `docker compose up -d` 失敗：保留 Docker 錯誤並回傳非零結束碼。
- API 未在期限內健康：記錄錯誤並回傳非零結束碼。

## 測試

- 使用假的 Docker 可執行檔驗證等待重試與 Compose 啟動參數。
- 使用本機測試 HTTP server 驗證 health check。
- 驗證安裝腳本產生正確的排程工作動作、觸發條件與設定，不實際寫入系統排程。
- 驗證 Compose 兩個服務都有重啟政策。

## 範圍

不變更 Extension 權限、API 回應、資料庫 schema、資料蒐集範圍或來源規則。
