# Administrative Unit Aliases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the requested NPTU administrative-unit aliases, official administrative-unit knowledge document, and regression tests without changing the existing RAG architecture.

**Architecture:** Reuse the existing keyword_search.aliases map and KeywordAliasResolver for announcement keyword expansion. Extend the RAG system prompt so administrative-unit questions use search_documents, and add a Markdown/YAML official snapshot using the existing document-ingestion path. No database schema or resolver code changes are needed.

**Tech Stack:** Python 3, Pytest, Pydantic metadata validation, PyYAML, Markdown knowledge documents, Docker Compose API container.

## Global Constraints

- All user-visible text and test descriptions use Traditional Chinese.
- Official answers must use sources returned from the database; do not generate source URLs in prompts or code.
- The only administrative-unit source URL is https://www.nptu.edu.tw/p/412-1000-86.php?Lang=zh-tw.
- Do not add Chrome permissions or collect passwords, Cookies, student IDs, grades, or national ID numbers.
- Do not add a database schema change; document ingestion uses the existing tables and command.
- Keep the existing 電科系＝電腦科學與人工智慧學系 rule and its explicit prohibition against interpreting it as 電腦與通訊學系.
- The aliases apply to the existing announcement keyword expansion and chat prompt guidance; do not add a global RAG preprocessor.
- Do not duplicate the 研發處 mapping.

---

## File Map

- Modify services/api/tests/test_keyword_search.py: regression coverage for every administrative alias through the existing KeywordAliasResolver.
- Modify services/api/tests/test_rag.py: regression coverage for administrative-unit routing and representative alias rules in SYSTEM_INSTRUCTIONS.
- Create services/api/tests/test_administrative_units_knowledge.py: metadata and content assertions for the new official snapshot.
- Modify data/sources/announcements.yaml: add the 28 requested administrative alias keys under the existing keyword_search.aliases mapping.
- Modify services/api/src/nptu_assistant/rag/prompts.py: add administrative-unit routing and alias instructions.
- Create data/official-documents/nptu-administrative-units.md: official administrative-unit hierarchy and listed subunits.
- Create data/official-documents/nptu-administrative-units.yaml: source and ingestion metadata.

## Task 1: Write failing regression tests

**Files:**
- Modify: services/api/tests/test_keyword_search.py
- Modify: services/api/tests/test_rag.py
- Create: services/api/tests/test_administrative_units_knowledge.py

**Interfaces:**
- Consumes: Existing load_keyword_search_config, KeywordAliasResolver, SYSTEM_INSTRUCTIONS, DocumentMetadata, and parse_document interfaces.
- Produces: Failing tests that specify the complete requested alias map, prompt behavior, official source metadata, and required administrative-unit names.

- [ ] **Step 1: Add all administrative aliases to the existing parameterized resolver test.**

Append these cases to the existing pytest parameter list in test_keyword_search.py:

    ("計網中心", "計算機與網路中心"),
    ("職推處", "職涯發展暨教育推廣處"),
    ("研發處", "研究發展處"),
    ("生輔組", "生活輔導組"),
    ("衛生組", "衛生保健組"),
    ("衛保組", "衛生保健組"),
    ("軍訓室", "軍訓暨校安中心"),
    ("軍安中心", "軍訓暨校安中心"),
    ("生動組", "學生活動發展組"),
    ("學諮中心", "學生諮商中心"),
    ("原資中心", "原住民族學生資源中心"),
    ("法制組", "行政法制組"),
    ("校發組", "校務發展組"),
    ("校研中心", "校務研究中心"),
    ("校友組", "校友服務組"),
    ("技合組", "技術合作組"),
    ("學發組", "學術發展組"),
    ("育成中心", "創新育成中心"),
    ("國際處", "國際事務處"),
    ("國合組", "國際合作組"),
    ("國文組", "國際學生組"),
    ("外生組", "國際學生組"),
    ("大陸組", "大陸事務組"),
    ("職輔組", "職涯輔導組"),
    ("進修組", "進修教學組"),
    ("推廣中心", "推廣教育中心"),
    ("場館組", "場館營運組"),
    ("競賽組", "競賽活動組"),

- [ ] **Step 2: Add a failing prompt regression test.**

Append this test to test_rag.py:

    def test_system_instructions_define_administrative_unit_aliases() -> None:
        assert "詢問行政單位、處室、組、中心或室時，使用 search_documents" in SYSTEM_INSTRUCTIONS
        assert "計網中心＝計算機與網路中心" in SYSTEM_INSTRUCTIONS
        assert "校友組＝校友服務組" in SYSTEM_INSTRUCTIONS
        assert "育成中心＝創新育成中心" in SYSTEM_INSTRUCTIONS
        assert "國文組、外生組＝國際學生組" in SYSTEM_INSTRUCTIONS
        assert "推廣中心＝推廣教育中心" in SYSTEM_INSTRUCTIONS

- [ ] **Step 3: Create a failing official knowledge-document test.**

Create services/api/tests/test_administrative_units_knowledge.py:

    from __future__ import annotations

    from pathlib import Path

    import yaml

    from nptu_assistant.ingestion.metadata import DocumentMetadata
    from nptu_assistant.ingestion.parsers import parse_document

    WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
    DOCUMENT = WORKSPACE_ROOT / "data/official-documents/nptu-administrative-units.md"
    SIDECAR = WORKSPACE_ROOT / "data/official-documents/nptu-administrative-units.yaml"

    def test_administrative_units_knowledge_document_has_official_metadata_and_units():
        metadata = DocumentMetadata.model_validate(
            yaml.safe_load(SIDECAR.read_text(encoding="utf-8"))
        )
        text = parse_document(DOCUMENT)

        assert str(metadata.source_url) == "https://www.nptu.edu.tw/p/412-1000-86.php?Lang=zh-tw"
        assert metadata.document_type == "administrative_units_snapshot"
        for office in (
            "校長室", "秘書室", "教務處", "學務處", "總務處",
            "研究發展處", "國際事務處", "職涯發展暨教育推廣處",
            "主計室", "人事室", "圖書館", "計算機與網路中心", "體育室",
        ):
            assert office in text
        for unit in (
            "行政法制組", "生活輔導組", "衛生保健組", "軍訓暨校安中心",
            "創新育成中心", "國際學生組", "推廣教育中心", "競賽活動組",
        ):
            assert unit in text

- [ ] **Step 4: Run the new tests and verify they fail for missing behavior.**

Run from C:\Users\lukes\OneDrive\文件\nptu-campus-assistant:

    services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_keyword_search.py services\api\tests\test_rag.py services\api\tests\test_administrative_units_knowledge.py -q

Expected: failures for the new administrative aliases, missing administrative prompt rules, and missing administrative knowledge-document files. Existing unrelated tests must not be removed or changed to hide failures.

## Task 2: Implement alias configuration and chat rules

**Files:**
- Modify: data/sources/announcements.yaml
- Modify: services/api/src/nptu_assistant/rag/prompts.py

**Interfaces:**
- Consumes: The failing tests from Task 1 and the existing KeywordAliasResolver/SYSTEM_INSTRUCTIONS interfaces.
- Produces: Alias expansion and chat guidance for all requested administrative names.

- [ ] **Step 1: Add the exact administrative aliases to announcements.yaml.**

Under keyword_search.aliases, add one entry for every key below. Keep 研發處 exactly once:

    計網中心: 計算機與網路中心
    職推處: 職涯發展暨教育推廣處
    研發處: 研究發展處
    生輔組: 生活輔導組
    衛生組: 衛生保健組
    衛保組: 衛生保健組
    軍訓室: 軍訓暨校安中心
    軍安中心: 軍訓暨校安中心
    生動組: 學生活動發展組
    學諮中心: 學生諮商中心
    原資中心: 原住民族學生資源中心
    法制組: 行政法制組
    校發組: 校務發展組
    校研中心: 校務研究中心
    校友組: 校友服務組
    技合組: 技術合作組
    學發組: 學術發展組
    育成中心: 創新育成中心
    國際處: 國際事務處
    國合組: 國際合作組
    國文組: 國際學生組
    外生組: 國際學生組
    大陸組: 大陸事務組
    職輔組: 職涯輔導組
    進修組: 進修教學組
    推廣中心: 推廣教育中心
    場館組: 場館營運組
    競賽組: 競賽活動組

- [ ] **Step 2: Add administrative-unit routing and alias rules to prompts.py.**

Keep the existing academic-unit routing sentence and add this sentence immediately after it:

    使用者詢問行政單位、處室、組、中心或室名稱時，使用 search_documents，以資料庫中的官方行政單位文件作為回答來源。

Add this complete administrative alias sentence after the existing department alias sentence:

    行政單位縮寫必須依下列規則理解：計網中心＝計算機與網路中心；職推處＝職涯發展暨教育推廣處；研發處＝研究發展處；生輔組＝生活輔導組；衛生組、衛保組＝衛生保健組；軍訓室、軍安中心＝軍訓暨校安中心；生動組＝學生活動發展組；學諮中心＝學生諮商中心；原資中心＝原住民族學生資源中心；法制組＝行政法制組；校發組＝校務發展組；校研中心＝校務研究中心；校友組＝校友服務組；技合組＝技術合作組；學發組＝學術發展組；育成中心＝創新育成中心；國際處＝國際事務處；國合組＝國際合作組；國文組、外生組＝國際學生組；大陸組＝大陸事務組；職輔組＝職涯輔導組；進修組＝進修教學組；推廣中心＝推廣教育中心；場館組＝場館營運組；競賽組＝競賽活動組。若資料庫中的官方文件沒有充分資訊，必須明確回答資料不足，不得自行推測。

- [ ] **Step 3: Run the alias and prompt tests.**

    services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_keyword_search.py services\api\tests\test_rag.py -q

Expected: all tests in both files pass, including every academic and administrative alias case.

## Task 3: Add and validate the official administrative knowledge snapshot

**Files:**
- Create: data/official-documents/nptu-administrative-units.md
- Create: data/official-documents/nptu-administrative-units.yaml
- Test: services/api/tests/test_administrative_units_knowledge.py

**Interfaces:**
- Consumes: The existing document parser and DocumentMetadata model.
- Produces: A database-ingestable official snapshot whose sidecar source is the user-provided official NPTU administrative-unit page.

- [ ] **Step 1: Create the Markdown snapshot with the official hierarchy.**

Create the document with title # 國立屏東大學行政單位清單, an introductory sentence identifying it as a 2026-07-13 snapshot, and the official source URL. Include these exact headings and entries:

    ## 校長室
    - 校長室
    - 副校長室

    ## 秘書室
    - 秘書室
    - 行政法制組
    - 校務發展組
    - 校務研究中心
    - 校友服務組

    ## 教務處
    - 教務處
    - 課務組
    - 註冊組
    - 綜合業務組
    - 教學發展組
    - 教學資源中心

    ## 學務處
    - 學務處
    - 生活輔導組
    - 衛生保健組
    - 軍訓暨校安中心
    - 學生活動發展組
    - 學生諮商中心
    - 原住民族學生資源中心

    ## 總務處
    - 總務處
    - 文書組
    - 事務組
    - 出納組
    - 營繕組
    - 保管組
    - 環安組

    ## 研究發展處
    - 研究發展處
    - 技術合作組
    - 學術發展組
    - 創新育成中心

    ## 國際事務處
    - 國際事務處
    - 國際合作組
    - 國際學生組
    - 大陸事務組

    ## 職涯發展暨教育推廣處
    - 職涯發展暨教育推廣處
    - 職涯輔導組
    - 招生組
    - 進修教學組
    - 推廣教育中心

    ## 主計室
    - 主計室

    ## 人事室
    - 人事室
    - 第一組
    - 第二組

    ## 圖書館
    - 圖書館

    ## 計算機與網路中心
    - 計算機與網路中心
    - 行政組
    - 網路組
    - 系統組

    ## 體育室
    - 體育室
    - 場館營運組
    - 競賽活動組

- [ ] **Step 2: Create the YAML sidecar metadata.**

Create data/official-documents/nptu-administrative-units.yaml:

    title: 國立屏東大學行政單位清單
    source_url: https://www.nptu.edu.tw/p/412-1000-86.php?Lang=zh-tw
    unit: 國立屏東大學
    published_at: 2026-07-13
    document_type: administrative_units_snapshot
    version: "2026-07-13"

- [ ] **Step 3: Run the knowledge-document test.**

    services\api\.venv\Scripts\python.exe -m pytest services\api\tests\test_administrative_units_knowledge.py -q

Expected: the metadata validates, the parser reads the Markdown, and all required headings/units are found.

## Task 4: Run the complete verification suite

**Files:**
- Verify: all changed files from Tasks 1–3.

**Interfaces:**
- Consumes: The complete implementation and existing API test suite.
- Produces: Fresh evidence for test status, formatting, and document-ingestion readiness.

- [ ] **Step 1: Run all API tests.**

    services\api\.venv\Scripts\python.exe -m pytest services\api\tests -q

Expected: zero failures; report any existing skips or warnings without hiding them.

- [ ] **Step 2: Check patch formatting and duplicate rule count.**

    git diff --check
    $yaml = Get-Content -Raw -Encoding UTF8 -LiteralPath 'data/sources/announcements.yaml'
    if (($yaml | Select-String -Pattern '(?m)^    研發處:' -AllMatches).Matches.Count -ne 1) { throw '研發處 alias must appear exactly once' }
    git status --short

Expected: git diff --check exits successfully, the duplicate check does not throw, and only the intended feature files are modified.

- [ ] **Step 3: Rebuild the API container and verify health.**

    docker compose up -d --build api
    docker compose ps api

Expected: the API container is running and reports healthy according to the existing compose health check.

- [ ] **Step 4: Ingest the new official document into the database.**

    docker compose exec -T api nptu-assistant ingest-documents

Expected: nptu-administrative-units.md is created or updated successfully with no failed documents. If the container cannot reach the configured database or LLM services, report the exact failure instead of claiming ingestion completed.

- [ ] **Step 5: Commit the implementation after fresh verification.**

    git add data/sources/announcements.yaml services/api/src/nptu_assistant/rag/prompts.py services/api/tests/test_keyword_search.py services/api/tests/test_rag.py services/api/tests/test_administrative_units_knowledge.py data/official-documents/nptu-administrative-units.md data/official-documents/nptu-administrative-units.yaml
    git commit -m "feat: add administrative unit aliases"

Only commit after the complete test suite, git diff --check, container health check, and document-ingestion result have been inspected.
