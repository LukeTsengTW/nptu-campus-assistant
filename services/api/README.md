# NPTU Assistant API

此 Python 3.12 package 包含 FastAPI、資料庫、文件匯入、公告爬蟲與 RAG 服務。完整啟動方式請參閱 repository 根目錄的 `README.md`。

公告來源設定位於 `../../data/sources/announcements.yaml`。單位公告由 `UnitSourceResolver` 將正式名稱或別名解析到已啟用來源；獎學金等主題則由後端 `source_routes` 固定路由到指定來源；共通 HTML 列表由 `nptu_html_list` 依 typed selectors 解析。模型不能傳入 URL、host、selector 或內部來源名稱。

手動執行所有已啟用來源：

```powershell
uv run nptu-assistant crawl-announcements
```

正式執行前須套用 Alembic migration；來源最後成功時間與 canonical URL scope 保存在 `sources` table。新增來源、爬取安全限制與 fixture 規範請參閱 `../../docs/data-sources.md`，單元、PostgreSQL integration 與 opt-in live smoke 指令請參閱 `../../docs/testing.md`。
