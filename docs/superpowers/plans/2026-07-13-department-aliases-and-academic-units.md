# 科系縮寫與學術單位知識庫實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將指定 NPTU 科系縮寫固定解析為正式名稱，並把官方學術單位清單加入既有文件知識庫匯入流程。

**Architecture:** 公告搜尋繼續從 YAML alias 設定進行查詢正規化；一般聊天從固定 system instructions 得到同一份語意規則。學術單位以 Markdown 加 YAML sidecar 形式加入既有 `data/official-documents/`，由原有文件匯入服務產生可檢索 chunks 與資料庫來源。

**Tech Stack:** Python 3.12、pytest、Pydantic、YAML、Markdown、既有 FastAPI RAG ingestion pipeline。

## Global Constraints

- 所有使用者可見文字使用繁體中文。
- Extension 不得包含秘密金鑰。
- 所有正式回答必須附帶資料來源。
- 來源 URL 必須來自資料庫，不得由 LLM 自行生成。
- 爬蟲只能存取 Allowlist 內的官方來源。
- 所有資料庫變更必須使用 Migration；本次不需要資料庫 schema 變更。
- 每個新功能必須包含測試。
- 不得聲稱本專案為國立屏東大學官方產品。

---

### Task 1: 新增科系縮寫規則與回歸測試

**Files:**
- Modify: `data/sources/announcements.yaml` 的 `keyword_search.aliases`
- Modify: `services/api/src/nptu_assistant/rag/prompts.py` 的 `SYSTEM_INSTRUCTIONS`
- Modify: `services/api/tests/test_keyword_search.py`
- Modify: `services/api/tests/test_rag.py`

**Interfaces:**
- `load_keyword_search_config(Path) -> KeywordSearchConfig` 讀取 YAML alias 設定。
- `KeywordAliasResolver.normalize(text: str) -> str` 將別名替換為正式名稱。
- `SYSTEM_INSTRUCTIONS: str` 是 `ChatService` 傳給 LLM 的固定系統規則。

- [ ] **Step 1: 先加入縮寫設定測試**

在 `services/api/tests/test_keyword_search.py` 加入參數化測試，透過實際 YAML 驗證下列 mapping：

```python
@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("電科系", "電腦科學與人工智慧學系"),
        ("資工系", "資訊工程學系"),
        ("電通系", "電腦與通訊學系"),
        ("資管系", "資訊管理學系"),
        ("機器人系", "智慧機器人學系"),
        ("智機系", "智慧機器人學系"),
        ("商大數系", "商業大數據學系"),
        ("行流系", "行銷與流通管理學系"),
        ("休閒系", "休閒事業經營學系"),
        ("不動系", "不動產經營學系"),
        ("不動產系", "不動產經營學系"),
        ("企管系", "企業管理學系"),
        ("國貿系", "國際經營與貿易學系"),
        ("財金系", "財務金融學系"),
        ("會計系", "會計學系"),
        ("教育系", "教育學系"),
        ("特教系", "特殊教育學系"),
        ("幼教系", "幼兒教育學系"),
        ("視藝系", "視覺藝術學系"),
        ("音樂系", "音樂學系"),
        ("文創系", "文化創意產業學系"),
        ("社發系", "社會發展學系"),
        ("中文系", "中國語文學系"),
        ("應日系", "應用日語學系"),
        ("應英系", "應用英語學系"),
        ("英語系", "英語學系"),
        ("英文系", "英語學系"),
        ("原民專班", "文化發展學士學位學程原住民專班"),
        ("科傳系", "科學傳播學系"),
        ("應化系", "應用化學系"),
        ("化學系", "應用化學系"),
        ("應物系", "應用物理系"),
        ("物理系", "應用物理系"),
        ("應數系", "應用數學系"),
        ("數學系", "應用數學系"),
        ("體育系", "體育學系"),
    ],
)
def test_keyword_aliases_normalize_requested_department_names(alias, canonical):
    config = load_keyword_search_config(WORKSPACE_ROOT / "data/sources/announcements.yaml")
    assert KeywordAliasResolver(config.aliases).normalize(alias) == canonical
```

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest services\\api\\tests\\test_keyword_search.py::test_keyword_aliases_normalize_requested_department_names -q`

Expected: FAIL because the new aliases are not all present in the YAML configuration.

- [ ] **Step 2: 先加入 system instructions 回歸測試**

在 `services/api/tests/test_rag.py` 匯入 `SYSTEM_INSTRUCTIONS`，加入：

```python
def test_system_instructions_define_department_aliases_without_electrical_department_ambiguity():
    assert "電科系＝電腦科學與人工智慧學系" in SYSTEM_INSTRUCTIONS
    assert "不得解讀為電腦與通訊學系" in SYSTEM_INSTRUCTIONS
    assert "資工系＝資訊工程學系" in SYSTEM_INSTRUCTIONS
    assert "機器人系、智機系＝智慧機器人學系" in SYSTEM_INSTRUCTIONS
    assert "英語系、英文系＝英語學系" in SYSTEM_INSTRUCTIONS
```

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest services\\api\\tests\\test_rag.py::test_system_instructions_define_department_aliases_without_electrical_department_ambiguity -q`

Expected: FAIL because the new system prompt block is not present.

- [ ] **Step 3: 將所有 aliases 寫入 YAML**

在 `keyword_search.aliases` 保留既有 alias，並新增本次指定的 36 個別名：

```yaml
    電科系: 電腦科學與人工智慧學系
    資工系: 資訊工程學系
    電通系: 電腦與通訊學系
    資管系: 資訊管理學系
    機器人系: 智慧機器人學系
    智機系: 智慧機器人學系
    商大數系: 商業大數據學系
    行流系: 行銷與流通管理學系
    休閒系: 休閒事業經營學系
    不動系: 不動產經營學系
    不動產系: 不動產經營學系
    企管系: 企業管理學系
    國貿系: 國際經營與貿易學系
    財金系: 財務金融學系
    會計系: 會計學系
    教育系: 教育學系
    特教系: 特殊教育學系
    幼教系: 幼兒教育學系
    視藝系: 視覺藝術學系
    音樂系: 音樂學系
    文創系: 文化創意產業學系
    社發系: 社會發展學系
    中文系: 中國語文學系
    應日系: 應用日語學系
    應英系: 應用英語學系
    英語系: 英語學系
    英文系: 英語學系
    原民專班: 文化發展學士學位學程原住民專班
    科傳系: 科學傳播學系
    應化系: 應用化學系
    化學系: 應用化學系
    應物系: 應用物理系
    物理系: 應用物理系
    應數系: 應用數學系
    數學系: 應用數學系
    體育系: 體育學系
```

- [ ] **Step 4: 將相同規則寫入 system instructions**

在 `SYSTEM_INSTRUCTIONS` 的一般回答規則後加入一段完整 mapping，至少包含：電科系、資工系、電通系、資管系、機器人系、智機系、商大數系、行流系、休閒系、不動系、不動產系、企管系、國貿系、財金系、會計系、教育系、特教系、幼教系、視藝系、音樂系、文創系、社發系、中文系、應日系、應英系、英語系、英文系、原民專班、科傳系、應化系、化學系、應物系、物理系、應數系、數學系、體育系，並明確寫出「電科系不得解讀為電腦與通訊學系」。

- [ ] **Step 5: 執行兩組測試確認轉綠**

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest services\\api\\tests\\test_keyword_search.py::test_keyword_aliases_normalize_requested_department_names services\\api\\tests\\test_rag.py::test_system_instructions_define_department_aliases_without_electrical_department_ambiguity -q`

Expected: PASS。

### Task 2: 加入官方學術單位知識文件與匯入測試

**Files:**
- Create: `data/official-documents/nptu-academic-units.md`
- Create: `data/official-documents/nptu-academic-units.yaml`
- Create: `services/api/tests/test_academic_units_knowledge.py`

**Interfaces:**
- `parse_document(Path) -> str` 解析 Markdown。
- `DocumentMetadata.model_validate(dict)` 驗證 sidecar 的官方 metadata。
- `DocumentIngestionService.run() -> IngestionSummary` 由既有流程匯入文件。

- [ ] **Step 1: 先新增知識文件契約測試**

建立測試，驗證 sidecar、官方 URL、主要學院與指定縮寫所需的正式名稱：

```python
from pathlib import Path

import yaml

from nptu_assistant.ingestion.metadata import DocumentMetadata
from nptu_assistant.ingestion.parsers import parse_document


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = WORKSPACE_ROOT / "data/official-documents/nptu-academic-units.md"
SIDECAR = WORKSPACE_ROOT / "data/official-documents/nptu-academic-units.yaml"


def test_academic_units_knowledge_document_has_official_metadata_and_departments():
    metadata = DocumentMetadata.model_validate(yaml.safe_load(SIDECAR.read_text(encoding="utf-8")))
    text = parse_document(DOCUMENT)

    assert str(metadata.source_url) == "https://www.nptu.edu.tw/p/412-1000-2972.php?Lang=zh-tw"
    assert metadata.document_type == "academic_units_snapshot"
    for college in ("管理學院", "資訊學院", "教育學院", "人文社會學院", "理學院", "國際學院", "大武山學院"):
        assert college in text
    for department in ("電腦科學與人工智慧學系", "電腦與通訊學系", "資訊工程學系", "智慧機器人學系", "應用數學系", "體育學系"):
        assert department in text
```

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest services\\api\\tests\\test_academic_units_knowledge.py -q`

Expected: FAIL because the knowledge files do not exist yet.

- [ ] **Step 2: 建立 Markdown 學術單位文件**

建立以學院為標題的 Markdown，收錄三張圖片中的所有學院、系所、學程與中心；內容明確標示為官方頁面快照，並保留圖片中的「含碩士班」及停招註記，不額外推測圖片未提供的資料。

- [ ] **Step 3: 建立 YAML sidecar**

寫入：

```yaml
title: 國立屏東大學學術單位清單
source_url: https://www.nptu.edu.tw/p/412-1000-2972.php?Lang=zh-tw
unit: 國立屏東大學
published_at: 2026-07-13
document_type: academic_units_snapshot
version: "2026-07-13"
```

- [ ] **Step 4: 執行知識文件測試確認轉綠**

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest services\\api\\tests\\test_academic_units_knowledge.py -q`

Expected: PASS。

### Task 3: 完整回歸驗證

**Files:**
- Read: `services/api/tests/test_keyword_search.py`
- Read: `services/api/tests/test_rag.py`
- Read: `services/api/tests/test_academic_units_knowledge.py`

- [ ] **Step 1: 執行定向 API 測試**

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest services\\api\\tests\\test_providers.py services\\api\\tests\\test_rag.py services\\api\\tests\\test_keyword_search.py services\\api\\tests\\test_services.py -q`

Expected: exit code 0，且沒有 failed 或 error。

- [ ] **Step 2: 執行 API 完整測試**

Run: `services\\api\\.venv\\Scripts\\python.exe -m pytest`

Expected: exit code 0；需要 PostgreSQL 或 live 網路的測試只能依既有設定顯示 skipped，不得刪除或忽略失敗。

- [ ] **Step 3: 檢查變更範圍**

Run: `git diff --check; git status --short`

Expected: 沒有 whitespace error；變更只包含 alias、system prompt、學術單位文件、測試與本計畫／設計文件。
