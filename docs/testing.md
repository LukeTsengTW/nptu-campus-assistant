# 測試指南

## Python

```powershell
cd services/api
uv sync --extra dev
uv run pytest
```

純單元測試不依賴資料庫；涉及 pgvector、migration、repository 的測試使用 Docker PostgreSQL，不以 SQLite 代替。

## Extension

```powershell
corepack pnpm install
corepack pnpm --filter @nptu/extension test
corepack pnpm --filter @nptu/extension build
```

## 整合驗證

```powershell
docker compose up -d --build
docker compose exec api alembic -c /app/alembic.ini upgrade head
Invoke-RestMethod http://127.0.0.1:8000/health
```

啟用真正的 PostgreSQL/pgvector 整合測試：

```powershell
$env:DATABASE_URL="postgresql+psycopg://nptu:nptu-development-only@127.0.0.1:5432/nptu_assistant"
$env:RUN_POSTGRES_INTEGRATION="1"
cd services/api
uv run pytest ../../tests/integration
```

未設定 `RUN_POSTGRES_INTEGRATION=1` 時，該組測試會明確顯示 skipped，不會改用 SQLite。GitHub Actions 會啟動 `pgvector/pgvector:pg17`，執行兩次 migration、兩次 seed、完整整合測試、OpenAPI drift 檢查與 Extension test/build。

所有自動化測試使用 Fake LLM 與 Fake Embedding Provider。live OpenAI 與 NPTU smoke tests 必須另外標示，不能成為預設測試的必要條件。

一般官方網頁搜尋的主要回歸測試在 `tests/test_semantic_site_search.py`，使用固定 HTML fixture、deterministic embedding provider 與 fake discovery，覆蓋中文無空格、不同用語、對話追問、父頁面相關性傳遞、warning 語意、正常零結果、外部 URL／資源拒絕與禁止招生種類專用 production branch；預設測試不依賴 NPTU live network。

啟用 NPTU 關鍵字公告搜尋 live smoke test：

```powershell
$env:RUN_LIVE_KEYWORD_SEARCH="1"
cd services/api
uv run pytest tests/test_live_keyword_search.py -q
```

此測試會請求 NPTU 官網，驗證 session、搜尋表單與結果 DOM；不寫入資料庫。未設定 `RUN_LIVE_KEYWORD_SEARCH=1` 時顯示 skipped。

啟用資訊學院單位公告來源 live smoke test：

```powershell
$env:RUN_LIVE_UNIT_ANNOUNCEMENT_SOURCE="1"
cd services/api
uv run pytest tests/test_live_unit_announcement_source.py -q
```

此測試只讀取 `data/sources/announcements.yaml`、檢查 robots、下載資訊學院列表並驗證目前 selectors 與 canonical URL host；不建立 repository，也不寫入資料庫。未設定環境變數時明確顯示 skipped，CI 不依賴外部網站可用性。
