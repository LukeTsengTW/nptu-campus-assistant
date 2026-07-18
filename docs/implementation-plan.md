# NPTU 校務資訊助理 MVP 實作計畫

更新日期：2026-07-10

## 專案目標

建立一個非官方 Chrome Manifest V3 擴充功能。使用者瀏覽 `nptu.edu.tw` 或其子網域時，可從右下角的 Shadow DOM 聊天介面查詢已匯入的國立屏東大學官方文件，或搜尋官方網站公告。所有正式回答都必須附上資料庫內可追溯的官方來源。

固定資料流：

```text
Chrome Extension content UI
  -> MV3 service worker / typed API client
  -> FastAPI modular monolith
  -> PostgreSQL / pgvector
  -> replaceable LLM and embedding providers
```

## 第一版範圍

- WXT、React、TypeScript Strict Mode 與 Shadow DOM Extension。
- FastAPI、Pydantic、SQLAlchemy、Alembic、PostgreSQL、pgvector API。
- PDF、HTML、Markdown、TXT 官方文件匯入與 sidecar YAML metadata。
- NPTU 官方總覽 RSS/XML adapter、本機 fixture adapter 與可擴充 crawler protocol。
- 文件向量／trigram 混合檢索、公告日期／關鍵字檢索、OpenAI/Fake providers。
- 所有答案的官方來源清單、資料不足結果、警告與非官方免責聲明。
- Docker Compose、seed、fixtures、單元／整合／Extension 測試與完整開發文件。

## 明確不包含

- 教師或課堂評價、個人校務資料、登入、成績、學號、畢業學分試算、選課與表單操作。
- 行動 App、Firefox、管理後台 GUI、社群、多租戶、付費、訂閱與聊天紀錄同步。
- 背景 queue、正式排程平台與雲端正式部署。

## 技術選型與理由

- `pnpm` workspace：管理 WXT app 與 TypeScript shared contracts。
- WXT + React：提供 Manifest V3 entrypoints、service worker 與 `createShadowRootUi`。
- 局部 CSS：Shadow DOM 已隔離樣式，使用固定 px 尺度避免宿主頁面的 rem 影響。
- Python 3.12 + uv：鎖定 FastAPI 服務與 CLI 的可重現依賴。
- SQLAlchemy 2 + Alembic：所有 schema 變更都有 migration。
- PostgreSQL + pgvector + pg_trgm：支援向量檢索、中文 substring/trigram 與結構化日期查詢。
- OpenAI Responses API + provider interfaces：正式文字生成可替換，測試不依賴 live API。
- `text-embedding-3-small` 1536 維：與 pgvector 欄位維度固定對齊。

## 系統與模組架構

```text
apps/extension/                WXT content UI、background 與 UI tests
services/api/                  FastAPI modular monolith 與 Python tests
  src/nptu_assistant/
    api/                       routers、schemas、errors、middleware
    core/                      settings、logging、security、rate limiting
    db/                        SQLAlchemy models、session、repositories
    ingestion/                 parsers、metadata、cleaning、chunking
    crawlers/                  protocols、HTTP policy、adapters
    rag/                       routing、retrieval、evidence policy、chat
    providers/                 OpenAI/Fake LLM 與 embedding providers
packages/shared/               由 OpenAPI 產生的 TypeScript contracts
database/migrations/           Alembic migration scripts
database/seeds/                可重複執行的來源 seed
data/official-documents/       本機官方文件與 sidecar metadata
data/fixtures/                 文件與公告 HTML/XML fixtures
docs/                          架構、來源、隱私與測試文件
tests/integration/             PostgreSQL/pgvector 端到端情境
```

## API 設計

### `GET /health`

- 回傳 `status: ok | degraded | unhealthy`。
- checks 包含 database、llm、embeddings。
- LLM 未設定時回 200/degraded；資料庫不可用時回 503/unhealthy。

### `POST /v1/chat`

- 輸入：`{"question": string}`，trim 後 1–2000 字元。
- `answer_type`：`official_document | announcement | insufficient_information`。
- `confidence`：`high | medium | low`。
- `sources` 的 URL 只能由資料庫取得，`source_type` 固定為 `official`。

### `GET /v1/announcements`

- query：`q`、`unit`、`date_from`、`date_to`、`page=1`、`page_size=20`。
- page size 上限 100，結果依 `published_at DESC`。
- 回傳 `items`、`page`、`page_size`、`total`。

### 管理端點

- `POST /v1/admin/ingest/documents` 只掃描固定資料目錄。
- `POST /v1/admin/crawl/announcements` 只接受設定檔中的 source names。
- 使用 `X-Admin-Key`；development 預設啟用，其他環境預設關閉。

### 共通政策

- 統一錯誤格式：`{"error":{"code","message","details","request_id"}}`。
- chat、announcement、admin 每 IP 每分鐘預設限制 20、60、5 次。
- CORS 僅允許明確 allowlist，不接受萬用字元。
- JSON logging 不記錄問題全文、環境秘密或 API key。

## 資料庫設計

- `sources`：官方來源設定、單位、類型、爬取開關與週期。
- `documents`：metadata、raw text、hash、版本、`is_current` 與 `supersedes_document_id`。
- `document_chunks`：順序、內容、1536 維 embedding、token count。
- `announcements`：標題、單位、分類、發布／截止日期、正文、URL、hash 與 last crawled time。
- `(canonical_url, content_hash)` 防止文件重複；同 URL 內容變更時建立新版本。
- 公告 URL 唯一；hash 變更時更新同一公告。
- 建立 HNSW cosine、發布日期、來源／單位／日期與 title/body trigram indexes。

## 官方文件匯入流程

1. 掃描固定目錄內的 PDF、HTML、Markdown、TXT。
2. 讀取同名 YAML sidecar；驗證 title、source URL、unit、document type、version 與日期。
3. 驗證 HTTPS NPTU allowlist；不允許捏造缺少的 URL。
4. 清除 script、iframe、style、隱藏內容與導覽雜訊。
5. 對正規化正文計算 SHA-256；相同內容直接跳過。
6. 依標題與段落切割約 700 tokens、重疊 100 tokens。
7. 以 embedding provider 產生 1536 維向量。
8. 在單一 transaction 中保存文件版本與 chunks；embedding 失敗不留下半成品。

## 公告爬取流程

- 第一個真實來源為 `https://www.nptu.edu.tw/p/503-1000-1044.php?Lang=zh-tw`。
- 解析 RSS/XML 的 title、link、description、pubDate、author，再 follow NPTU allowlist detail URL。
- detail 失敗時保留已清理的 feed description 並記錄 warning，不假裝 detail 已驗證。
- 每次執行檢查 robots、connect/read timeout 5/15 秒、最多三次退避重試、每 host 至少一秒間隔。
- 本機 fixture adapter 覆蓋可重現的列表、detail、錯誤與更新流程。

## RAG 檢索流程

1. 以規則識別「公告／最新／截止／報名」意圖；模糊問題同時搜尋文件與公告。
2. 文件向量與 trigram 各取前 20，使用 RRF `k=60` 合併後取前 6。
3. 證據分數低於 0.35 或無來源時直接回資料不足。
4. 分數 0.35–0.55、0.55–0.75、0.75 以上依序對應 low、medium、high。
5. Provider 僅接收編號化來源內容；結構化輸出只能回 answer、used source IDs 與 warning。
6. 後端驗證 source IDs 並從資料庫組裝 URL；無合法 ID 時改回資料不足。
7. 目前有效版本優先，日期較新者優先；來源衝突時將衝突放入 warning。

## Chrome Extension 架構

- Content script 僅建立 Shadow DOM UI，不讀取或上傳原始頁面 HTML。
- UI 透過 typed message 呼叫 MV3 service worker；service worker 負責 API fetch。
- API base URL 由 `WXT_API_BASE_URL` 在建置時設定並轉成精確 host permission。
- `chrome.storage.local` 只保存 panel open state；聊天訊息只留在當前 UI 記憶體。
- 固定顯示非官方免責聲明；來源卡片以安全的新分頁連結開啟。

## 資安與隱私設計

- 不蒐集密碼、Cookie、學號、成績、身分證字號或完整頁面 HTML。
- Extension 不含秘密；OpenAI key 只在後端 `.env.local`／環境變數。
- 對問題長度、query parameters、source names、metadata 與外部 URL 全面驗證。
- 爬取內容永遠視為不可信資料，清理可執行／隱藏內容後才進入 RAG。
- ORM／參數化查詢、固定來源 allowlist、精確 CORS、管理金鑰與 rate limit。

## 測試策略

- Python unit：chunking、metadata、hash、日期、截止日、去重、排序、evidence policy、Fake providers 與 schemas。
- Crawler：list/detail、缺欄位、異常 HTML、robots、retry、timeout、URL allowlist 與內容更新。
- API：五個 endpoints、錯誤 envelope、管理金鑰、CORS、rate limit、health branches。
- Extension：按鈕、面板、送出、載入、回答、來源、錯誤、清除、manifest permissions。
- Integration：PostgreSQL/pgvector fixture 匯入、chat 附來源、公告爬取與日期排序。
- 所有自動化測試使用 Fake providers，不依賴真實 OpenAI API。

## 里程碑與驗收

- [x] **M0－安全憑證與計畫文件**
  - [x] 安全建立 OpenAI key 並寫入未追蹤的 `.env.local`。
  - [x] 建立秘密與產物忽略規則。
  - [x] 建立本實作計畫。
  - [x] 執行秘密追蹤檢查。
- [x] **M1－Monorepo、文件與本機環境**
  - 驗收：workspace metadata、Compose、`.env.example`、README、AGENTS.md 與指定 docs 齊全。
- [x] **M2－資料庫、Migration 與基礎 API**
  - [x] Models、首個 migration、seed、health 三分支、CORS、rate limit、錯誤 envelope、request ID 與遮罩 logging 已完成並通過單元測試。
  - [x] 本機 PostgreSQL/pgvector online migration 與 seed 均重跑兩次成功；Alembic current 與 head 一致。
- [x] **M3－官方文件匯入**
  - 驗收：四種 parser、metadata、清理、hash、標題邊界 chunk、Fake/OpenAI embedding、首次建立與相同內容跳過已測；版本外鍵與 transaction 的 PostgreSQL 驗證由整合測試執行。
- [x] **M4－公告爬蟲**
  - 驗收：fixture list/detail、重複、更新、異常、timeout/retry 測試通過；live 狀態如實記錄。
- [x] **M5－RAG 與完整 API**
  - 驗收：文件／公告回答附來源；無來源與未知 source ID 不產生答案。
- [x] **M6－Chrome Extension**
  - 驗收：build 成功、manifest 最小權限、所有指定 UI 測試通過。
- [x] **M7－整合驗證與交付**
  - [x] Python 單元/API 測試、Extension 測試、typecheck/build、manifest、Alembic offline SQL、OpenAPI 型別產生與 live NPTU smoke test 已完成。
  - [x] PostgreSQL/pgvector 整合測試與 CI 已建立，且 schema drift 由 CI 檢查。
  - [x] 本機 Compose build/up、db/API health、online migration、seed 與 PostgreSQL/pgvector 整合測試均已實際通過。

### 2026-07-10 驗證紀錄

- `pytest`：53 passed；3 個 PostgreSQL/pgvector tests 因 `RUN_POSTGRES_INTEGRATION` 未啟用而明確 skipped。
- Extension／shared：7 tests passed；recursive typecheck 與 production build 通過。
- Manifest：MV3；permission 僅 `storage`；content matches 僅 NPTU；host permission 僅本機 API origin。
- Alembic offline SQL：成功產生 vector、pg_trgm、四張表、`vector(1536)`、HNSW/trigram indexes 與公告 warning 欄位。
- OpenAPI 與 TypeScript schema 連續重生 hash 一致；秘密 pattern 掃描 0 筆，`.env.local` 已忽略且未追蹤。
- NPTU live smoke：feed 20 筆、抽樣 detail 清理後 602 字元；未執行 live OpenAI 推論。

### 2026-07-11 Docker／PostgreSQL 補充驗證

- Docker Desktop 4.81.0、Engine 29.6.1、Compose 5.2.0；使用 `desktop-linux` WSL 2 backend。
- 修正 Dockerfile build order：先以 `--no-install-project` 安裝鎖定依賴，再複製 README/source 並安裝本地 package；回歸測試通過。
- `docker compose up -d --build` 成功，`db` 與 `api` health 均為 `healthy`。
- Online migration 連跑兩次、seed 連跑兩次皆 exit 0；Alembic current/head 均為 `20260710_0001`。
- PostgreSQL/pgvector 整合測試：3 passed；`GET /health` 回傳 HTTP 200、`status=ok`、database/LLM/embeddings checks 均正常。
- 啟用 PostgreSQL 整合測試後重跑完整 Python suite：57 passed。期間修正 heading overlap 測試將 token 上限誤判為字元上限的斷言，實際 overlap 為設定值 10 tokens。

## 已知風險與限制

### 2026-07-18 全校學術單位通用查詢

- 建立 typed `official_units.yaml`：66 筆、64 active、2 discontinued；64 active homepage 由 NPTU 官方學術單位頁連結建立。
- alias、homepage、host、seed、公告策略集中管理；configured listing 由目錄生成 crawler source。
- `UnitSourceResolver` 分離單位辨識與來源能力；支援 configured listing、unit-scoped search、同單位 cache、明確 insufficient。
- homepage query 使用 config-backed evidence；document／announcement live flow 使用 unit allowlist 與 seed，不得跨單位補結果。
- 共用 intent classifier 處理 generic latest 與 topic；Fake Provider、production prompt、deterministic tests 使用同一 registry／詞彙。
- 新增全 registry audit、代表單位矩陣、scope／污染／warning tests、`RUN_NPTU_LIVE_SMOKE=1` opt-in smoke。

- 本機已使用 bundled pnpm 11.7、Python 3.12、uv 與 Docker Desktop 4.81.0 完成可執行驗證。
- NPTU feed 與 detail HTML 可能改版；2026-07-10 live smoke test 成功不保證未來結構不變。
- MVP rate limiter 為單程序記憶體實作，不適用多副本正式部署。
- 管理工作同步執行，大型匯入與排程留待下一階段。
- 任何未實際執行的測試、build、migration、live crawl 或 OpenAI call 都不得宣稱成功。
