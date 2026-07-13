# 科系縮寫與學術單位知識庫設計

## 目標

讓 chatbot 能穩定將 NPTU 常用科系縮寫解析為指定正式名稱，並能從官方學術單位文件回答系所、學院、學程與中心相關問題。

## 設計

1. `data/sources/announcements.yaml` 的 `keyword_search.aliases` 維持作為公告關鍵字搜尋的別名設定，補齊本次指定的所有單一縮寫與多重寫法。
2. `services/api/src/nptu_assistant/rag/prompts.py` 增加明確的科系縮寫規則，讓一般聊天在決定回答或選擇工具前採用相同的正式名稱；「電科系」明確禁止解讀為「電腦與通訊學系」。
3. 新增 `data/official-documents/nptu-academic-units.md` 與同名 YAML sidecar，收錄三張圖片所示的學術單位，並以國立屏東大學官方學術單位頁面作為 `source_url`。沿用既有文件匯入流程，不新增資料表或 Migration。

## 來源與資料日期

- 官方來源：`https://www.nptu.edu.tw/p/412-1000-2972.php?Lang=zh-tw`
- 文件是 2026-07-13 擷取的官方頁面快照；sidecar 使用同日作為快照日期與版本，並不宣稱該頁面於該日發布。

## 測試

- 以參數化測試驗證所有指定縮寫可由實際 YAML 設定正規化為正式名稱。
- 驗證聊天系統提示詞含有完整規則及「電科系」的排除歧義文字。
- 驗證學術單位 Markdown 與 sidecar 可由既有文件匯入格式讀取，並含官方來源及主要學術單位名稱。

## 限制

- 不新增 Chrome 權限、不接觸密碼、Cookie、學號、成績或身分證字號。
- 不新增或修改資料庫 schema；正式匯入仍由既有 `ingest-documents` 指令執行。
- 回答來源 URL 仍由資料庫中的文件 metadata 建立，不由模型自行產生。
