"""建立 MVP 資料表與檢索索引。"""

from collections.abc import Sequence

from alembic import op
from pgvector.sqlalchemy import Vector
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260710_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("unit", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("crawl_enabled", sa.Boolean(), nullable=False),
        sa.Column("crawl_interval_minutes", sa.Integer(), nullable=False),
        *_timestamps(),
    )
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("canonical_url", sa.String(2048), nullable=False),
        sa.Column("document_type", sa.String(100), nullable=False),
        sa.Column("published_at", sa.Date()),
        sa.Column("effective_from", sa.Date()),
        sa.Column("effective_to", sa.Date()),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("supersedes_document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id")),
        *_timestamps(),
        sa.UniqueConstraint("canonical_url", "content_hash", name="uq_documents_url_hash"),
    )
    op.create_index(
        "ix_documents_current_url",
        "documents",
        ["canonical_url"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("document_id", "sequence", name="uq_document_chunks_sequence"),
    )
    op.create_index(
        "ix_document_chunks_embedding_hnsw",
        "document_chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "announcements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("unit", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100)),
        sa.Column("published_at", sa.Date(), nullable=False),
        sa.Column("deadline_at", sa.Date()),
        sa.Column("canonical_url", sa.String(2048), nullable=False, unique=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("warning", sa.Text()),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
    )
    op.execute("CREATE INDEX ix_announcements_published_at_desc ON announcements (published_at DESC)")
    op.create_index(
        "ix_announcements_source_unit_date",
        "announcements",
        ["source_id", "unit", "published_at"],
    )
    op.create_index(
        "ix_announcements_title_trgm",
        "announcements",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_announcements_body_trgm",
        "announcements",
        ["body"],
        postgresql_using="gin",
        postgresql_ops={"body": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_table("announcements")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("sources")
