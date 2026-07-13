from pathlib import Path

import yaml


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]


def test_compose_preserves_api_key_from_env_file() -> None:
    compose = yaml.safe_load((WORKSPACE_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    api = compose["services"]["api"]

    assert api["env_file"][0]["path"] == ".env.local"
    assert "OPENAI_API_KEY" not in api["environment"]
    assert api["environment"]["DATABASE_URL"].startswith("postgresql+psycopg://")


def test_dockerfile_installs_dependencies_before_local_project() -> None:
    dockerfile = (WORKSPACE_ROOT / "services/api/Dockerfile").read_text(encoding="utf-8")

    dependency_sync = dockerfile.index("uv sync --frozen --no-dev --no-install-project")
    readme_copy = dockerfile.index("COPY services/api/README.md")
    source_copy = dockerfile.index("COPY services/api/src")
    project_sync = dockerfile.rindex("uv sync --frozen --no-dev")

    assert dependency_sync < readme_copy < project_sync
    assert dependency_sync < source_copy < project_sync


def test_live_announcement_source_uses_twenty_items_and_hourly_refresh() -> None:
    payload = yaml.safe_load(
        (WORKSPACE_ROOT / "data/sources/announcements.yaml").read_text(encoding="utf-8")
    )
    source = next(item for item in payload["sources"] if item["name"] == "nptu-overview")

    assert source["max_items"] == 20
    assert source["crawl_interval_minutes"] == 60
