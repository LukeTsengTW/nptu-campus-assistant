from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from nptu_assistant.db.models import Document, DocumentChunk


_POSTGRES_FLOW_FILE = "test_postgres_flow.py"


@pytest.fixture(autouse=True)
def isolate_postgres_flow_documents(request: pytest.FixtureRequest) -> Iterator[None]:
    """Prevent document fixtures from leaking into later integration suites.

    ``test_postgres_flow.py`` exercises retrieval against shared PostgreSQL and
    creates documents with deliberately identical fake embeddings.  The CI job
    later reruns the P4 focused suite against the same service database, so any
    documents left behind can change ranking independently of the P4 fixture.

    Snapshotting existing document IDs preserves migration/seed data and removes
    only rows created by the current postgres-flow test.  The fixture is scoped
    by test file so unrelated integration benchmarks do not pay cleanup cost or
    have their datasets modified.
    """

    test_path = Path(str(request.node.path))
    database_url = os.getenv("DATABASE_URL")
    enabled = (
        os.getenv("RUN_POSTGRES_INTEGRATION") == "1"
        and database_url is not None
        and test_path.name == _POSTGRES_FLOW_FILE
    )
    if not enabled:
        yield
        return

    engine = create_engine(database_url, pool_pre_ping=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        existing_document_ids = tuple(session.scalars(select(Document.id)).all())

    try:
        yield
    finally:
        with factory.begin() as session:
            new_document_ids = select(Document.id)
            if existing_document_ids:
                new_document_ids = new_document_ids.where(
                    Document.id.not_in(existing_document_ids)
                )
            session.execute(
                delete(DocumentChunk).where(
                    DocumentChunk.document_id.in_(new_document_ids)
                )
            )
            delete_documents = delete(Document)
            if existing_document_ids:
                delete_documents = delete_documents.where(
                    Document.id.not_in(existing_document_ids)
                )
            session.execute(delete_documents)
        engine.dispose()
