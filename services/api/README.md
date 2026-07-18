# NPTU Assistant API

此 Python 3.12 package 包含 FastAPI、資料庫、文件匯入、公告爬蟲與 RAG 服務。完整啟動方式請參閱 repository 根目錄的 `README.md`。

全校學術單位目錄位於 `../../data/sources/official_units.yaml`，集中正式名稱、alias、狀態、homepage、allowed host、site-search seeds 與公告策略。全校／主題公告與共用搜尋限制位於 `../../data/sources/announcements.yaml`。`UnitSourceResolver` 分開處理「已知單位」與「是否有 configured listing」；沒有固定 listing、但有可信 homepage 的單位使用 host-scoped search。模型不能傳入 URL、host、selector 或內部來源名稱。

手動執行所有已啟用來源：

```powershell
uv run nptu-assistant crawl-announcements
```

正式執行前須套用 Alembic migration；來源最後成功時間與 canonical URL scope 保存在 `sources` table。新增來源、爬取安全限制與 fixture 規範請參閱 `../../docs/data-sources.md`，單元、PostgreSQL integration 與 opt-in live smoke 指令請參閱 `../../docs/testing.md`。
