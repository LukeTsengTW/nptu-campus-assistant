"""新增文件 trigram 索引與共享網站搜尋快取。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260719_0004"
down_revision: str | None = "20260713_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_document_chunks_content_trgm",
        "document_chunks",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_documents_title_trgm",
        "documents",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_table(
        "site_search_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_site_search_cache_expires_at",
        "site_search_cache",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_site_search_cache_expires_at", table_name="site_search_cache")
    op.drop_table("site_search_cache")
    op.drop_index("ix_documents_title_trgm", table_name="documents")
    op.drop_index("ix_document_chunks_content_trgm", table_name="document_chunks")
