"""separate fetched and ingested page state with lease fencing

Revision ID: 20260803_0008
Revises: 20260802_0007
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_0008"
down_revision: str | None = "20260802_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older site-map rows used NULL to mean "not scheduled".  The claim
    # query must never interpret that sentinel as immediately due, so give
    # every legacy row an explicit bounded retry time before enabling the
    # stricter frontier query.
    op.execute(
        sa.text(
            """
            UPDATE site_pages
            SET next_crawl_at = CURRENT_TIMESTAMP + INTERVAL '30 seconds'
            WHERE next_crawl_at IS NULL
            """
        )
    )
    op.add_column("site_pages", sa.Column("ingestion_content_hash", sa.String(64)))
    op.add_column(
        "site_pages",
        sa.Column(
            "ingestion_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column("site_pages", sa.Column("ingestion_error", sa.String(1000)))
    op.add_column("site_crawl_attempts", sa.Column("final_url", sa.String(2000)))
    op.add_column(
        "site_pages",
        sa.Column(
            "announcement_ingestion_status",
            sa.String(20),
            nullable=False,
            server_default="not_applicable",
        ),
    )
    op.add_column(
        "site_pages",
        sa.Column("announcement_ingestion_error", sa.String(1000)),
    )
    op.add_column(
        "site_pages",
        sa.Column("ingestion_lease_token", postgresql.UUID(as_uuid=True)),
    )
    op.add_column("site_pages", sa.Column("ingestion_lease_owner", sa.String(200)))
    op.add_column(
        "site_pages",
        sa.Column("ingestion_lease_expires_at", sa.DateTime(timezone=True)),
    )
    op.create_check_constraint(
        "ck_site_pages_ingestion_status",
        "site_pages",
        "ingestion_status IN ('pending', 'failed', 'partial', 'success')",
    )
    op.create_check_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        "announcement_ingestion_status IN ('not_applicable', 'pending', 'failed', 'success')",
    )
    op.create_index(
        "ix_site_pages_ingestion_status", "site_pages", ["ingestion_status"]
    )
    op.create_index(
        "ix_site_pages_ingestion_lease_expires_at",
        "site_pages",
        ["ingestion_lease_expires_at"],
    )
    op.create_index(
        "ix_site_pages_announcement_ingestion_status",
        "site_pages",
        ["announcement_ingestion_status"],
    )
    op.create_index(
        "ix_site_pages_host_crawl_lease_expires_at",
        "site_pages",
        ["host", "crawl_lease_expires_at"],
    )

    op.execute(
        sa.text(
            """
            UPDATE site_pages
            SET announcement_ingestion_status = 'pending'
            WHERE page_type IN ('announcement_listing', 'announcement_detail')
            """
        )
    )

    # Preserve known successful document state while leaving unmatched fetched
    # hashes recoverable as pending.
    op.execute(
        sa.text(
            """
            UPDATE site_pages AS page
            SET ingestion_status = 'success',
                ingestion_content_hash = page.content_hash
            WHERE page.content_hash IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM documents AS document
                  WHERE document.canonical_url = page.canonical_url
                    AND document.content_hash = page.content_hash
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("site_crawl_attempts", "final_url")
    op.drop_index(
        "ix_site_pages_ingestion_lease_expires_at", table_name="site_pages"
    )
    # IF EXISTS keeps downgrade safe for databases upgraded by the first
    # development revision of 0008 before this performance index was added.
    op.execute(
        sa.text("DROP INDEX IF EXISTS ix_site_pages_host_crawl_lease_expires_at")
    )
    op.drop_index(
        "ix_site_pages_announcement_ingestion_status", table_name="site_pages"
    )
    op.drop_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        type_="check",
    )
    op.drop_index("ix_site_pages_ingestion_status", table_name="site_pages")
    op.drop_constraint(
        "ck_site_pages_ingestion_status", "site_pages", type_="check"
    )
    op.drop_column("site_pages", "ingestion_lease_expires_at")
    op.drop_column("site_pages", "ingestion_lease_owner")
    op.drop_column("site_pages", "ingestion_lease_token")
    op.drop_column("site_pages", "ingestion_error")
    op.drop_column("site_pages", "announcement_ingestion_error")
    op.drop_column("site_pages", "announcement_ingestion_status")
    op.drop_column("site_pages", "ingestion_status")
    op.drop_column("site_pages", "ingestion_content_hash")
