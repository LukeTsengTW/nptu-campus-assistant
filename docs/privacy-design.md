# 隱私與資安設計

## 不蒐集資料

本工具不蒐集或傳送 Cookie、密碼、學號、成績、身分證字號、完整頁面 HTML 或其他個人校務資料。Extension 不持久化對話。

## 秘密管理

- OpenAI 與管理金鑰只存在後端環境變數或被 Git 忽略的本機 env file。
- `.env.example` 只有欄位名稱與開發用非秘密預設值。
- Logs 不包含問題全文、Authorization、API key 或完整環境變數。

## 不可信內容

- 爬取文字只能作為資料，不能成為 system/developer instruction。
- HTML 清除 script、iframe、style、hidden、`aria-hidden=true`、`display:none` 與 `visibility:hidden`。
- 外部 URL 必須通過 HTTPS 與 `nptu.edu.tw` suffix allowlist，禁止任意抓取。

## 使用者提示

Extension 必須固定顯示：「本工具並非國立屏東大學官方系統。重要申請資格、期限與規定請以原始官方公告為準。」
