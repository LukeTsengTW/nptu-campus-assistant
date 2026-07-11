# 官方資料來源

## 已選定公告來源

- 名稱：NPTU 官方總覽
- Feed：`https://www.nptu.edu.tw/p/503-1000-1044.php?Lang=zh-tw`
- 官方 host：`www.nptu.edu.tw` 與 feed 回傳的 `*.nptu.edu.tw` detail URL
- 2026-07-10 live smoke test：crawler 依序檢查 robots 後，feed 成功解析 20 筆；抽樣的 `cec.nptu.edu.tw` detail 頁成功清理出 602 字元。feed 可回傳 title、link、description、pubDate、author。

每次正式執行仍必須重新檢查 robots。若 live smoke test 失敗，只能記錄限制，不得以 fixture 宣稱真實來源成功。

## 新增來源

1. 在 `data/sources/announcements.yaml` 新增 allowlist 設定。
2. 建立獨立 adapter，不得把網站 selector 加入通用 crawler。
3. 先保存真實頁面的去識別 fixture，再撰寫 list/detail tests。
4. 驗證 robots、timeout、retry、interval 與 canonical URL。

## 官方文件

每份文件需搭配同名 YAML sidecar。`source_url` 必須是 HTTPS NPTU 官方網址；缺少時拒絕匯入。
