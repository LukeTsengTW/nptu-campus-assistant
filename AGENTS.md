# NPTU 校務資訊助理長期規則

- 所有使用者可見文字使用繁體中文。
- Extension 不得包含秘密金鑰。
- 不得蒐集密碼、Cookie、學號、成績或身分證字號。
- 所有正式回答必須附帶資料來源。
- 來源 URL 必須來自資料庫，不得由 LLM 自行生成。
- 無充分資料時必須明確回答資料不足。
- 爬蟲只能存取 Allowlist 內的官方來源。
- 新增 Chrome 權限前必須說明理由。
- 所有資料庫變更必須使用 Migration。
- 每個新功能必須包含測試。
- 不得聲稱本專案為國立屏東大學官方產品。
- 除非後續任務明確要求，不得實作教師評價或個人校務資料功能。
- 不得忽略測試錯誤、刪除測試以求通過，或宣稱未實際驗證的功能已完成。

## 程式碼查詢工具優先順序

- 探索、除錯、重構或檢視程式碼時，必須先使用 `code-review-graph` MCP 工具；優先從 `get_minimal_context_tool` 開始，再依需求使用 `semantic_search_nodes_tool`、`query_graph_tool`、`get_impact_radius_tool` 或 `get_review_context_tool`。
- 若 graph 尚未建立、MCP 不可用、查詢結果為空，或結果與問題不相關，才改用傳統搜尋：`rg` / `rg --files`，再讀取必要檔案。
- 不得把 graph 的空結果視為已找到答案；必須確認結果與目前問題相關，否則依上述規則回退搜尋。
