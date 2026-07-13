# NPTU 校務資訊助理

非官方 Chrome 擴充功能 MVP：在國立屏東大學相關網域提供官方文件問答與最新公告搜尋。所有正式回答都附上資料庫內的官方來源。

> 本工具並非國立屏東大學官方系統。重要申請資格、期限與規定請以原始官方公告為準。

## 系統需求

- Node.js 24、Corepack、pnpm 11
- Python 3.12、uv
- Docker Desktop 與 Docker Compose v2
- Chrome 或 Chromium

## 安裝相依套件

```powershell
corepack pnpm install
cd services/api
uv sync --frozen --extra dev
```

`.venv` 僅能在建立它的作業系統使用；不得將 Docker、WSL 或其他 Linux 環境建立的 `.venv` 複製到 Windows。若 `services/api/.venv/pyvenv.cfg` 的 `home` 是 `/usr/local/bin`，請在 `services/api` 重建本機環境：

```powershell
Remove-Item -LiteralPath .venv -Recurse -Force
uv sync --frozen --extra dev
```

## 環境設定

複製 `.env.example` 為 `.env.local`，只在本機填入秘密。不得提交 `.env.local`。

主要欄位：

- `OPENAI_API_KEY`：只供後端使用。
- `OPENAI_TEXT_MODEL`：預設 `gpt-5.4-mini`。
- `OPENAI_EMBEDDING_MODEL`：預設 `text-embedding-3-small`。
- `ADMIN_API_KEY`：保護本機管理端點。
- `WXT_API_BASE_URL`：Extension 建置時的 API URL。

未設定 OpenAI key 時 API 仍會啟動，`/health` 顯示 degraded；自動化測試使用 Fake Providers。

## 啟動資料庫與後端

```powershell
docker compose up -d --build
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod 'http://127.0.0.1:8000/v1/announcements?page=1&page_size=20'
```

Compose 會等待 PostgreSQL healthy，執行 `alembic upgrade head` 後啟動 API。公告 API 回傳目前資料庫中已收錄的公告；初次啟動時，背景工作會檢查來源是否需要刷新。

### Windows 登入自動啟動

Docker Desktop 須保留「登入 Windows 時啟動」設定。接著在 repository 根目錄執行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows-autostart.ps1
```

此命令為目前使用者建立 `NPTU Campus Assistant Backend` 登入排程。排程會等待 Docker Desktop 引擎就緒、執行 `docker compose up -d`，再等待 API health check 成功。`db` 與 `api` 容器也會在 Docker 引擎重新啟動後自動恢復。

立即執行及檢查：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-nptu-assistant.ps1
Get-ScheduledTask -TaskName "NPTU Campus Assistant Backend"
Invoke-RestMethod http://127.0.0.1:8000/health
```

啟動日誌位於 `%LOCALAPPDATA%\NptuCampusAssistant\startup.log`。停用、重新啟用或移除排程：

```powershell
Disable-ScheduledTask -TaskName "NPTU Campus Assistant Backend"
Enable-ScheduledTask -TaskName "NPTU Campus Assistant Backend"
Unregister-ScheduledTask -TaskName "NPTU Campus Assistant Backend" -Confirm:$false
```

手動 migration 與 seed：

```powershell
cd services/api
uv run alembic -c ../../alembic.ini upgrade head
uv run nptu-assistant seed
```

## 匯入官方文件

1. 將 PDF、HTML、Markdown 或 TXT 放入 `data/official-documents/`。
2. 建立同名 YAML sidecar，至少提供 title、source_url、unit、document_type、version 與日期。
3. 執行：

```powershell
cd services/api
uv run nptu-assistant ingest-documents
```

## 執行公告爬蟲

```powershell
cd services/api
uv run nptu-assistant crawl-announcements
```

新增來源時，先更新 `data/sources/announcements.yaml`，再建立獨立 adapter 與 fixtures。不得傳入任意 URL。

API 啟動後每 60 秒檢查已啟用來源是否到期；`nptu-overview` 的實際刷新間隔由 `crawl_interval_minutes: 60` 控制。使用者查詢最新公告時也會先做相同檢查。未到期不會重新請求官網；到期時最多處理 RSS 前 20 則。需要立即刷新時，可執行上方的 `crawl-announcements` 指令。

刷新失敗時，系統保留最後成功收錄的資料並在回答附上警告。所有回答來源仍從資料庫產生，模型不能指定任意爬取 URL。

## 建置與載入 Extension

```powershell
corepack pnpm --filter @nptu/extension build
```

1. 開啟 `chrome://extensions`。
2. 啟用「開發人員模式」。
3. 選擇「載入未封裝項目」。
4. 指向 `apps/extension/.output/chrome-mv3/`。

Extension 只會在 `nptu.edu.tw` 與其子網域注入介面。

## 執行測試

```powershell
cd services/api
uv run pytest
cd ../..
corepack pnpm test
corepack pnpm build
```

詳細策略與整合測試請參閱 `docs/testing.md`。

若要執行真正的 PostgreSQL/pgvector 整合流程（不使用 SQLite）：

```powershell
docker compose up -d db
$env:DATABASE_URL="postgresql+psycopg://nptu:nptu-development-only@127.0.0.1:5432/nptu_assistant"
$env:RUN_POSTGRES_INTEGRATION="1"
cd services/api
uv run alembic -c ../../alembic.ini upgrade head
uv run pytest ../../tests/integration
```

更新 API schema 後，執行下列命令並提交兩個產物；CI 會以 `git diff --exit-code` 檢查 drift：

```powershell
cd services/api
uv run nptu-assistant export-openapi --output ../../packages/shared/openapi.json
cd ../..
corepack pnpm --filter @nptu/shared generate
```

## 文件

- `docs/implementation-plan.md`：里程碑與驗收。
- `docs/architecture.md`：系統邊界與資料流。
- `docs/data-sources.md`：官方來源與新增 adapter 流程。
- `docs/privacy-design.md`：隱私與安全限制。
- `docs/testing.md`：單元、整合與 build 驗證。

## 已知限制

- 管理操作同步執行，沒有外部 queue 或集中式排程平台；公告刷新使用 API process 內的背景工作。
- 公告刷新鎖是 process-local。部署多個 API worker 前，必須改用 PostgreSQL advisory lock 或獨立 scheduler worker，避免同一來源被不同 process 同時爬取。
- Rate limiter 為單 process 記憶體實作。
- NPTU 網站改版時需以真實 HTML 更新對應 adapter fixture 與 parser。
- 本專案不提供登入、成績、選課、個人校務資料、教師評價或代送表單功能。
