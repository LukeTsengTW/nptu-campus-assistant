# Windows Auto-start Implementation Plan

> **For Codex:** Execute inline with test-driven development. Preserve unrelated worktree changes.

**Goal:** Windows 登入後可靠啟動 Docker Compose 後端，等待 API 可用。

**Architecture:** Compose restart policy 提供容器層恢復；PowerShell 啟動器提供 Docker 就緒等待、Compose 啟動、API health 等待；排程安裝器提供登入觸發。

**Tech Stack:** Docker Compose、Windows PowerShell 5.1+、pytest。

---

### Task 1: 建立失敗測試

**Files:**
- Create: `tests/test_windows_autostart.py`

- [ ] 驗證 Compose restart policy。
- [ ] 驗證假的 Docker 前兩次失敗後，啟動器會重試並執行 `compose up -d`。
- [ ] 驗證 health endpoint 成功後回傳 0。
- [ ] 驗證排程定義的程式、引數、登入觸發與重試設定。
- [ ] 執行 `services\api\.venv\Scripts\python.exe -m pytest tests\test_windows_autostart.py`，確認因檔案不存在而失敗。

### Task 2: 實作 Compose 與啟動器

**Files:**
- Modify: `docker-compose.yml`
- Create: `scripts/start-nptu-assistant.ps1`

- [ ] 為 `db`、`api` 加上 `restart: unless-stopped`。
- [ ] 實作 Docker 條件輪詢、Compose 啟動、health 條件輪詢及日誌。
- [ ] 執行目標測試，確認啟動器測試通過。

### Task 3: 實作排程安裝器

**Files:**
- Create: `scripts/install-windows-autostart.ps1`

- [ ] 建立可測試的排程定義輸出模式。
- [ ] 建立或更新目前使用者的 AtLogOn 排程工作。
- [ ] 執行目標測試，確認排程測試通過。

### Task 4: 文件與完整驗證

**Files:**
- Modify: `README.md`

- [ ] 記錄安裝、立即啟動、停用與移除方式。
- [ ] 執行目標測試、API 測試、Extension 測試與 Compose config 驗證。
- [ ] 實際安裝排程工作，查詢工作定義。
- [ ] 啟動 Docker Desktop 後，驗證 `docker compose ps` 與 `/health`。
