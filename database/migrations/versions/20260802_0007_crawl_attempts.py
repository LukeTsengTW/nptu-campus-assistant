"""add crawl leases and per-attempt HTTP audit records

Revision ID: 20260802_0007
Revises: 20260723_0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_0007"
down_revision: str | None = "20260723_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "site_pages",
        sa.Column("crawl_lease_token", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "site_pages",
        sa.Column("crawl_lease_owner", sa.String(200)),
    )
    op.add_column(
        "site_pages",
        sa.Column("crawl_lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "site_pages",
        sa.Column("last_scheduled_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "site_pages",
        sa.Column("unchanged_streak", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "site_pages",
        sa.Column("changed_streak", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("site_pages", sa.Column("last_error_kind", sa.String(100)))
    op.add_column("site_pages", sa.Column("last_error_at", sa.DateTime(timezone=True)))
    op.add_column(
        "site_pages",
        sa.Column("last_retry_after_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_site_pages_crawl_lease_expires_at",
        "site_pages",
        ["crawl_lease_expires_at"],
    )
    op.create_index(
        "ix_site_pages_crawl_schedule",
        "site_pages",
        ["next_crawl_at", "crawl_status"],
    )
    op.create_index(
        "ix_site_pages_host_next_crawl_at",
        "site_pages",
        ["host", "next_crawl_at"],
    )
    op.create_index(
        "ix_site_pages_due_active_crawlable",
        "site_pages",
        ["next_crawl_at", "crawl_priority"],
        postgresql_where=sa.text(
            "is_active = true AND is_indexable = true "
            "AND crawl_status NOT IN ('blocked', 'excluded')"
        ),
    )

    op.create_table(
        "site_crawl_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "site_page_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("site_pages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", sa.String(200), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "outcome",
            sa.String(32),
            nullable=False,
            server_default="running",
        ),
        sa.Column("http_status", sa.Integer()),
        sa.Column("content_type", sa.String(255)),
        sa.Column("content_length", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("content_changed", sa.Boolean()),
        sa.Column("links_discovered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ingestion_performed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("etag", sa.String(500)),
        sa.Column("last_modified", sa.String(500)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_kind", sa.String(100)),
        sa.Column("error_message", sa.String(1000)),
        sa.CheckConstraint(
            "outcome IN ('running', 'success_changed', 'success_unchanged', "
            "'not_modified', 'failed_transient', 'failed_permanent', 'blocked', "
            "'excluded', 'lease_lost')",
            name="ck_site_crawl_attempts_outcome",
        ),
    )
    op.create_index(
        "ix_site_crawl_attempts_page_started_at",
        "site_crawl_attempts",
        ["site_page_id", "started_at"],
    )
    op.create_index(
        "ix_site_crawl_attempts_outcome",
        "site_crawl_attempts",
        ["outcome"],
    )
    op.create_index(
        "ix_site_crawl_attempts_lease_token",
        "site_crawl_attempts",
        ["lease_token"],
    )
    op.create_index(
        "ix_site_crawl_attempts_worker_started_at",
        "site_crawl_attempts",
        ["worker_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_site_crawl_attempts_worker_started_at",
        table_name="site_crawl_attempts",
    )
    op.drop_index(
        "ix_site_crawl_attempts_lease_token",
        table_name="site_crawl_attempts",
    )
    op.drop_index(
        "ix_site_crawl_attempts_outcome",
        table_name="site_crawl_attempts",
    )
    op.drop_index(
        "ix_site_crawl_attempts_page_started_at",
        table_name="site_crawl_attempts",
    )
    op.drop_table("site_crawl_attempts")
    op.drop_index(
        "ix_site_pages_due_active_crawlable",
        table_name="site_pages",
    )
    op.drop_index(
        "ix_site_pages_host_next_crawl_at",
        table_name="site_pages",
    )
    op.drop_index(
        "ix_site_pages_crawl_schedule",
        table_name="site_pages",
    )
    op.drop_index(
        "ix_site_pages_crawl_lease_expires_at",
        table_name="site_pages",
    )
    op.drop_column("site_pages", "crawl_lease_expires_at")
    op.drop_column("site_pages", "crawl_lease_owner")
    op.drop_column("site_pages", "crawl_lease_token")
    op.drop_column("site_pages", "last_retry_after_at")
    op.drop_column("site_pages", "last_error_at")
    op.drop_column("site_pages", "last_error_kind")
    op.drop_column("site_pages", "changed_streak")
    op.drop_column("site_pages", "unchanged_streak")
    op.drop_column("site_pages", "last_scheduled_at")
