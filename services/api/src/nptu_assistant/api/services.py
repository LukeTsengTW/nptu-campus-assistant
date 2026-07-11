from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from nptu_assistant.core.settings import Settings
from nptu_assistant.db.repositories import SqlAnnouncementRepository


class HealthService:
    def __init__(self, factory: sessionmaker[Session], settings: Settings) -> None:
        self._factory = factory
        self._settings = settings

    def check(self) -> dict[str, object]:
        try:
            with self._factory() as session:
                session.execute(select(1)).scalar_one()
            database = "ok"
        except Exception:
            database = "error"
        llm = "configured" if self._settings.is_llm_configured else "not_configured"
        embeddings = "configured" if self._settings.is_embedding_configured else "not_configured"
        status = "unhealthy" if database == "error" else (
            "ok" if llm == "configured" and embeddings == "configured" else "degraded"
        )
        return {
            "status": status,
            "checks": {"database": database, "llm": llm, "embeddings": embeddings},
        }


class AnnouncementService:
    def __init__(self, repository: SqlAnnouncementRepository) -> None:
        self._repository = repository

    def list(self, **kwargs: object) -> object:
        return self._repository.list(**kwargs)
