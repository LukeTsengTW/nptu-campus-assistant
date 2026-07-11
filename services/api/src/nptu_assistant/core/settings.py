from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


WORKSPACE_ROOT = Path(__file__).resolve().parents[5]


def resolve_workspace_path(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate.resolve() if candidate.is_absolute() else (WORKSPACE_ROOT / candidate).resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(WORKSPACE_ROOT / ".env.local", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://nptu:nptu-development-only@127.0.0.1:5432/nptu_assistant"
    openai_api_key: SecretStr | None = None
    openai_text_model: str = "gpt-5.4-mini"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dimensions: int = 1536
    llm_provider: Literal["openai", "fake"] = "openai"
    embedding_provider: Literal["openai", "fake"] = "openai"
    admin_api_enabled: bool | None = None
    admin_api_key: SecretStr = SecretStr("nptu-local-admin-change-me")
    cors_allowed_origins: str = "http://127.0.0.1:3000"
    crawler_config_path: str = "data/sources/announcements.yaml"
    crawler_user_agent: str = "NPTU-Campus-Assistant/0.1 (non-official local development)"
    crawler_request_interval_seconds: float = 1.0
    official_documents_path: str = "data/official-documents"

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        origins = [value.strip() for value in self.cors_allowed_origins.split(",") if value.strip()]
        if "*" in origins:
            raise ValueError("CORS 不允許萬用字元")
        if self.openai_embedding_dimensions != 1536:
            raise ValueError("MVP 的 embedding 維度必須為 1536")
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [value.strip() for value in self.cors_allowed_origins.split(",") if value.strip()]

    @property
    def has_openai_key(self) -> bool:
        return bool(self.openai_api_key and self.openai_api_key.get_secret_value().strip())

    @property
    def is_admin_enabled(self) -> bool:
        if self.admin_api_enabled is not None:
            return self.admin_api_enabled
        return self.app_env.lower() == "development"

    @property
    def is_llm_configured(self) -> bool:
        return self.llm_provider == "fake" or self.has_openai_key

    @property
    def is_embedding_configured(self) -> bool:
        return self.embedding_provider == "fake" or self.has_openai_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
