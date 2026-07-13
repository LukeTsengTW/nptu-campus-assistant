# Chat 與 RAG Tool Calling 重構

## 現況分析

1. Chat API endpoint 為 `POST /v1/chat`，位於 `services/api/src/nptu_assistant/main.py`。目前 request 只含 `question`，endpoint 直接呼叫 `chat_service.answer(payload.question)`。
2. OpenAI provider 位於 `services/api/src/nptu_assistant/providers/openai.py`。文字生成已使用 Responses API；模型由 `OPENAI_TEXT_MODEL` 設定，預設 `gpt-5.4-mini`。Embedding 與文字 provider 目前各自建立 OpenAI client。
3. `QuestionRoute` 與 `route_question()` 位於 `services/api/src/nptu_assistant/rag/routing.py`。
4. Routing 以固定公告與時效關鍵字將問題分類為 `DOCUMENT`、`ANNOUNCEMENT`、`MIXED`。
5. `SqlRetriever.search(question, route)` 的正式呼叫位置只有 `ChatService.answer()`；其他引用為相關測試。
6. Chat schemas 位於 `services/api/src/nptu_assistant/api/schemas.py`。目前 `ChatRequest` 只有 `question`；`ChatResponse` 包含 `answer`、`answer_type`、`confidence`、`sources`、`warning`。
7. Repository 目前沒有 conversation store；Extension 只在 React state 保存畫面訊息，每次 API request 都是獨立問題。
8. `Evidence` 位於 `services/api/src/nptu_assistant/rag/models.py`。`ChatService` 以模型輸出的 `used_source_ids` 對當輪 Evidence 做 allowlist，並由後端建立 `SourceReference`。既有 sanitizer 會移除內部 UUID。
9. 相關測試集中於 `services/api/tests/test_rag.py`、`test_providers.py`、`test_api.py`、`tests/integration/test_postgres_flow.py`，以及 Extension 的 API client／ChatWidget 測試。

## 舊 Chat request flow

```text
使用者 question
  -> route_question() 固定詞彙分類
  -> SqlRetriever.search(question, QuestionRoute)
  -> retriever 清除公告命令文字
  -> 文件／公告 SQL 檢索
  -> OpenAI Responses API 單次生成
  -> used_source_ids 後端過濾
  -> ChatResponse
```

## 目標 Chat request flow

```text
使用者 question + optional conversation_id
  -> 載入有限 conversation context
  -> OpenAI Responses API + strict function tools
  -> 驗證並執行 search_announcements / search_documents / get_announcement
  -> function_call_output 回傳模型；最多四輪
  -> strict grounded final answer
  -> Evidence/source allowlist 與文字 sanitizer
  -> 保存有限 conversation state
  -> ChatResponse + conversation_id
```

## 修改範圍

- 移除 retriever 的自然語言命令解析與主要 Chat flow 的 `QuestionRoute`。
- 將 `SqlRetriever` 改成結構化資料查詢介面；保留 cosine similarity、trigram similarity、RRF、`Document.is_current` 與 fixture 過濾。
- 在既有 OpenAI Responses provider 上加入正規化 model turn；由 `ChatService` 執行完整 tool loop。
- 新增 strict tool schemas、Pydantic 驗證、白名單 dispatcher、安全 tool result。
- 以 Alembic Migration 新增 24 小時 PostgreSQL conversation store。
- 擴充 Chat API 與 shared/Extension contract；不新增 Chrome 權限，不將秘密放入 Extension。
- 新增 Retriever、tool schema、orchestration、conversation、API 與 Extension 測試。

## 固定設計決策

- 模型維持 `gpt-5.4-mini`；沿用 `OPENAI_API_KEY` 與既有 provider 設定。
- Wiring 只建立一個 OpenAI client，文字與 embedding provider 共用。
- Tool rounds 上限為 4；公告與文件混合問題允許同輪多工具。
- `limit` 僅接受 1 到 20；模型面對超量要求必須使用 20 並說明上限。
- Conversation TTL 為 24 小時；prompt 最多 12 則訊息、16,000 字，最近兩組工具結果只保留必要來源 metadata。
- 模型產生的 source ID 或 URL 不具權威性；正式 sources 與可顯示 URL 一律由 Evidence registry 決定。

## 驗證紀錄

### 修改摘要

- `_GENERIC_ANNOUNCEMENT_TERMS`、`normalize_announcement_keyword()`、`QuestionRoute`、`route_question()`、`rag/routing.py` 已移除。
- `SqlRetriever` 現在只接受結構化 query、limit、sort、unit、date 與 announcement ID。
- Responses API 已改成最多四輪的 strict function tool loop；支援同輪多工具、結構化錯誤、未知工具拒絕、來源 allowlist。
- 新增 PostgreSQL conversation store、24 小時 TTL、有限 context、敏感文字落庫前遮蔽與 DELETE endpoint。
- Chat API／OpenAPI／shared types／Extension 已支援 `conversation_id`；Extension 清除對話時同步刪除 server state。
- OpenAI 文字與 embedding provider 由 wiring 注入同一個 OpenAI client；模型仍為 `gpt-5.4-mini`。

### 修改檔案

- Backend：`api/schemas.py`、`db/models.py`、`providers/{fake,openai,protocols}.py`、`rag/{conversation,models,prompts,retrieval,service,tools}.py`、`main.py`、`wiring.py`。
- Database：`database/migrations/versions/20260712_0002_chat_conversations.py`。
- Extension：`background.ts`、`ChatWidget.tsx`、`api-client.ts`、`messages.ts`、`storage.ts`。
- Contracts：`packages/shared/openapi.json`、`packages/shared/src/schema.d.ts`。
- Tests：Retriever、tools、provider、orchestration、conversation、API、Extension、PostgreSQL integration 測試。

### 保留的檢索能力

- `DocumentChunk.embedding.cosine_distance()` 向量相似度。
- `func.similarity()` 文件內容／標題與公告標題／內文排序。
- Reciprocal Rank Fusion 與既有分數合併方式。
- `Document.is_current` 過濾。
- 公告 fixture source SQL filter。

### Tool Calling Loop 位置

- `services/api/src/nptu_assistant/rag/service.py`：迴圈、dispatcher、round limit、Evidence registry、最終來源治理。
- `services/api/src/nptu_assistant/providers/openai.py`：Responses API transport、function-call output 正規化、strict final JSON、OpenAI 錯誤映射。
- `services/api/src/nptu_assistant/rag/tools.py`：strict schemas、Pydantic arguments、白名單工具執行、安全 JSON tool output。

### Conversation state

- `conversations` 與 `conversation_events` 儲存於 PostgreSQL；Migration revision `20260712_0002`。
- 24 小時 sliding TTL；過期資料於讀取時清除。
- Prompt 最多 12 則訊息、16,000 字；最近兩組工具結果只保存 ID、順序與正式 source metadata。
- 密碼、Cookie、學號、成績、身分證等敏感型態不保存原文；命中時整則事件改存遮蔽文字。

### 實際驗證結果

- Python unit/API suite：`80 passed, 5 skipped`；五項 skip 為需明確啟用 PostgreSQL 的 integration tests。
- PostgreSQL integration suite：`5 passed`。
- Extension／shared tests：`10 passed`；shared TypeScript compile 通過。
- Recursive TypeScript typecheck：通過。
- Recursive production build：通過；Chrome MV3 output 總計 `216.4 kB`。
- OpenAPI export 與 generated artifact 比對：`openapi_match=true`。
- Alembic offline SQL：成功產生兩個 conversation tables、indexes、cascade FK 與 revision update。
- PostgreSQL live revision：`20260712_0002 (head)`；conversation tables `2`、cascade FK `1`。
- `docker compose up -d --build`：成功；API 與 DB containers 均為 `healthy`。
- `GET /health`：`status=ok`；database、llm、embeddings 均為 `configured/ok`。
- Repository 沒有配置 Python formatter、linter、type checker，因此未虛構執行結果。TypeScript type checker 已實際執行。

### 限制與風險

- 預設測試未呼叫真人 OpenAI API；tool selection 與多輪案例使用 scripted／fake provider，未消耗 API 額度。
- 過期 conversation 採 request-time opportunistic cleanup；沒有額外排程清理工作。
- 敏感文字遮蔽採防禦性 pattern；不等同完整語意型 DLP。系統不提供個人校務資料功能，也不建立使用者身分欄位。
- Production tool selection 仍取決於 `gpt-5.4-mini` 遵循 system instructions；後端的 schema、round limit、dispatcher 與 source allowlist 提供第二層防護。
