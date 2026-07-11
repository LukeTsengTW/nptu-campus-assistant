from nptu_assistant.db.models import Announcement, Document, DocumentChunk, Source


def test_required_tables_and_vector_dimension() -> None:
    assert Source.__tablename__ == "sources"
    assert Document.__tablename__ == "documents"
    assert DocumentChunk.__tablename__ == "document_chunks"
    assert Announcement.__tablename__ == "announcements"
    assert DocumentChunk.__table__.c.embedding.type.dim == 1536
    assert Announcement.__table__.c.warning.nullable is True
