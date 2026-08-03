"""persist undated announcement terminal status

Revision ID: 20260803_0009
Revises: 20260803_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0009"
down_revision: str | None = "20260803_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "site_pages",
        sa.Column("ingestion_attempt_hash", sa.String(64), nullable=True),
    )
    # 0008 stored the attempted hash in ingestion_content_hash even when the
    # transaction failed. Preserve that recovery signal separately, while
    # restoring ingestion_content_hash to its documented meaning: the last
    # successfully persisted document hash. Do not rewrite partial rows because
    # their document may already have succeeded while announcement persistence
    # remained retryable.
    op.execute(
        sa.text(
            "UPDATE site_pages "
            "SET ingestion_attempt_hash = ingestion_content_hash, "
            "    ingestion_content_hash = NULL "
            "WHERE ingestion_status IN ('pending', 'failed') "
            "  AND ingestion_content_hash IS NOT NULL"
        )
    )
    op.drop_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        "announcement_ingestion_status IN "
        "('not_applicable', 'pending', 'failed', 'incomplete', 'success')",
    )
    # 0008 cannot represent terminal incomplete, so downgrade stores a
    # reserved, bounded marker in its existing warning column. Restore the
    # terminal state when the schema returns to 0009 instead of turning it
    # into a retryable pending row after a migration round-trip.
    op.execute(
        sa.text(
            "UPDATE site_pages "
            "SET announcement_ingestion_status = 'incomplete', "
            "    announcement_ingestion_error = COALESCE(NULLIF("
            "substring(announcement_ingestion_error FROM 21), ''), "
            "'公告項目缺少官方發布日期，已標記為 terminal incomplete') "
            "WHERE announcement_ingestion_status = 'pending' "
            "  AND announcement_ingestion_error LIKE 'terminal_incomplete:%'"
        )
    )


def downgrade() -> None:
    # Downgrade must leave rows valid under 0008. Preserve terminal incomplete
    # with a reserved marker in the existing warning column because 0008 has
    # no equivalent status value; upgrade restores it without making it
    # retryable.
    op.execute(
        sa.text(
            """
            UPDATE site_pages
            SET announcement_ingestion_status = 'pending',
                announcement_ingestion_error = LEFT(
                    'terminal_incomplete:' || COALESCE(
                        announcement_ingestion_error, ''
                    ),
                    1000
                )
            WHERE announcement_ingestion_status = 'incomplete'
            """
        )
    )
    op.execute(
        sa.text(
            "UPDATE site_pages "
            "SET ingestion_content_hash = COALESCE("
            "ingestion_content_hash, ingestion_attempt_hash) "
            "WHERE ingestion_status IN ('pending', 'failed')"
        )
    )
    op.drop_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        type_="check",
    )
    op.create_check_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        "announcement_ingestion_status IN "
        "('not_applicable', 'pending', 'failed', 'success')",
    )
    op.drop_column("site_pages", "ingestion_attempt_hash")
