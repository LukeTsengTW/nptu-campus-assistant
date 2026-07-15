# 官方資料來源

## 已啟用公告來源

### NPTU 官方總覽

- 設定名稱：`nptu-overview`
- Adapter：設定驅動的 `nptu_html_list`
- 列表：`https://www.nptu.edu.tw/p/422-1000-1044.php?Lang=zh-tw`
- 列表容器：`#pageptlist`
- 公告列：`table.listTB tbody tr`
- 日期：`td[data-th="日期"] .d-txt`
- 標題與連結：`.mtitle > a[href]`
- 單次上限：20 則
- 刷新間隔：60 分鐘
- Detail：啟用，URL 必須仍為 NPTU 官方 HTTPS 網址

### 資訊學院

- 設定名稱：`information-college-html`
- 單位與目前別名：`資訊學院`
- Adapter：設定驅動的 `nptu_html_list`
- 列表：`https://ccs.nptu.edu.tw/p/403-1025-1019-1.php?Lang=zh-tw`
- 來源 host allowlist：`ccs.nptu.edu.tw`
- 列表容器：`section.mb`
- 公告列：`.row.listBS`
- 日期：`i.mdate`
- 標題與連結：`.mtitle > a[href]`
- 連結屬性：`href`
- 單次上限：20 則
- 刷新間隔：60 分鐘
- Detail：停用；列表標題作為目前可檢索內容

資訊學院首頁的「最新公告」由 JavaScript 動態載入；設定改用首頁「更多最新公告」連結指向的同站靜態列表，讓安全 HTTP client 不需模擬瀏覽器或依賴 AJAX 私有流程即可取得完整公告列。

列表解析器只讀設定容器內的公告列，清理標題、解析日期、將相對連結轉為 canonical URL、拒絕非來源 host 的連結、依 URL 去重，再以日期由新到舊穩定排序。單一壞列會記錄結構化 warning 並跳過；缺少列表容器、完全沒有公告列或所有列都無效時，整個來源視為失敗，不能用部分結果覆寫成功快照。

## 單位解析與查詢範圍

`UnitSourceResolver` 合併來源設定中的 `unit`／`aliases` 與既有關鍵字別名。別名採最長、非重疊匹配，結果分為：

- `resolved`：唯一對應已啟用來源，只刷新與查詢該來源。
- `unknown`：文字看似單位但設定無法辨識，要求使用者提供正式名稱。
- `ambiguous`：同時出現多個單位或別名對應多個來源，列出穩定排序的候選單位。
- `unsupported`：能正規化為已知單位，但尚未設定已啟用的官方公告來源。
- `none`：沒有單位意圖，保留既有全校公告／關鍵字搜尋流程。

成功 crawl 會在同一交易中 upsert 公告、把本次 URL 全量寫入 `Source.canonical_urls`，並更新 `last_successful_crawl_at`；內容全部 unchanged 與手動 crawl 也遵循同一流程。成功空結果保存為空陣列；任一資料列或快照寫入失敗時整批回滾。單位查詢只檢索該快照中的 URL，限制與排序完成後才回傳最多 20 筆。

## 爬取安全限制

- 僅接受 NPTU 根網域或子網域的 HTTPS URL；拒絕 userinfo、非 443 port 與相似字尾網域。
- 每個 HTML 來源另有明確 `allowed_hosts`；初始 URL、每一次 redirect 與 detail URL 都重新驗證。
- 每次執行重新取得並檢查 `robots.txt`；無法確認允許時不繼續爬取。
- 連線 timeout 5 秒、整體 timeout 15 秒；timeout、傳輸錯誤與 HTTP 錯誤最多有限重試 3 次。
- 同 host 請求有節流間隔；單一 process 最多跟隨 5 次 redirect。
- 回應上限 2 MiB，超過即拒絕。
- Extension 不保存來源秘密，也不能要求 crawler 存取任意 URL。

## 新增或維護 HTML 來源

1. 在 `data/sources/announcements.yaml` 新增唯一的 `name`、正式 `unit`、必要 `aliases`、官方列表 URL、精確 `allowed_hosts`、selectors、interval 與 max items。
2. 優先使用 `nptu_html_list`；只有現有 typed selector schema 無法表達版型時才新增 adapter。
3. 在 `data/fixtures/announcements/` 保存不含個資與秘密的代表性列表 fixture。
4. 新增設定驗證、正常列、相對 URL、重複、壞列、缺 selector、越界 host、排序與 service scope tests。
5. HTML 改版時先更新 fixture 與紅燈測試，再調整 YAML selector 或 parser。
6. 以 opt-in live smoke 驗證真實 DOM 與 robots；live 失敗不得以 fixture 宣稱官網已驗證成功。

## 官方文件

每份文件需搭配同名 YAML sidecar。`source_url` 必須是 HTTPS NPTU 官方網址；缺少時拒絕匯入。正式回答的 URL 只能來自資料庫 evidence，不能由模型產生。
