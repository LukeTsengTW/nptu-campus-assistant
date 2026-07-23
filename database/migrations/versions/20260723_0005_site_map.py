"""建立可持久化的 NPTU 網頁地圖。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260723_0005"
down_revision: str | None = "20260719_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "site_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_url", sa.String(2048), nullable=False, unique=True),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("path", sa.String(2048), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("unit", sa.String(200)),
        sa.Column(
            "page_type",
            sa.String(50),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column(
            "discovery_source",
            sa.String(50),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "crawl_status",
            sa.String(30),
            nullable=False,
            server_default="discovered",
        ),
        sa.Column("http_status", sa.Integer()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("etag", sa.String(500)),
        sa.Column("last_modified", sa.String(500)),
        sa.Column(
            "last_discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_crawled_at", sa.DateTime(timezone=True)),
        sa.Column("last_successful_crawl_at", sa.DateTime(timezone=True)),
        sa.Column("last_changed_at", sa.DateTime(timezone=True)),
        sa.Column("next_crawl_at", sa.DateTime(timezone=True)),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("crawl_priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("minimum_depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_indexable", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        *_timestamps(),
    )
    op.create_index("ix_site_pages_host", "site_pages", ["host"])
    op.create_index("ix_site_pages_unit", "site_pages", ["unit"])
    op.create_index("ix_site_pages_page_type", "site_pages", ["page_type"])
    op.create_index("ix_site_pages_crawl_status", "site_pages", ["crawl_status"])
    op.create_index("ix_site_pages_next_crawl_at", "site_pages", ["next_crawl_at"])
    op.create_index(
        "ix_site_pages_active_indexable",
        "site_pages",
        ["is_active", "is_indexable"],
    )
    op.create_index(
        "ix_site_pages_host_priority",
        "site_pages",
        ["host", "crawl_priority"],
    )
    op.create_index(
        "ix_site_pages_title_trgm",
        "site_pages",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_site_pages_path_trgm",
        "site_pages",
        ["path"],
        postgresql_using="gin",
        postgresql_ops={"path": "gin_trgm_ops"},
    )

    op.create_table(
        "site_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("site_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("site_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("anchor_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("link_type", sa.String(30), nullable=False, server_default="unknown"),
        sa.Column(
            "first_discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        *_timestamps(),
        sa.UniqueConstraint(
            "source_page_id",
            "target_page_id",
            name="uq_site_links_source_target",
        ),
    )
    op.create_index("ix_site_links_source_page_id", "site_links", ["source_page_id"])
    op.create_index("ix_site_links_target_page_id", "site_links", ["target_page_id"])


def downgrade() -> None:
    op.drop_table("site_links")
    op.drop_index("ix_site_pages_path_trgm", table_name="site_pages")
    op.drop_index("ix_site_pages_title_trgm", table_name="site_pages")
    op.drop_index("ix_site_pages_host_priority", table_name="site_pages")
    op.drop_index("ix_site_pages_active_indexable", table_name="site_pages")
    op.drop_index("ix_site_pages_next_crawl_at", table_name="site_pages")
    op.drop_index("ix_site_pages_crawl_status", table_name="site_pages")
    op.drop_index("ix_site_pages_page_type", table_name="site_pages")
    op.drop_index("ix_site_pages_unit", table_name="site_pages")
    op.drop_index("ix_site_pages_host", table_name="site_pages")
    op.drop_table("site_pages")
