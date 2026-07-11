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
