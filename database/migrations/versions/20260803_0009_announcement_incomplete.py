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
    # Some development databases were stamped at 0008 before the announcement
    # ingestion columns and objects were added to that revision. Make 0009
    # compatible with both that schema and a fresh database built from the
    # current 0008 migration.
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "ADD COLUMN IF NOT EXISTS ingestion_attempt_hash VARCHAR(64)"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "ADD COLUMN IF NOT EXISTS announcement_ingestion_status "
            "VARCHAR(20) NOT NULL DEFAULT 'not_applicable'"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "ADD COLUMN IF NOT EXISTS announcement_ingestion_error VARCHAR(1000)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE site_pages "
            "SET announcement_ingestion_status = 'not_applicable' "
            "WHERE announcement_ingestion_status IS NULL"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "ALTER COLUMN announcement_ingestion_status SET DEFAULT 'not_applicable', "
            "ALTER COLUMN announcement_ingestion_status SET NOT NULL"
        )
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
    # Recreate both checks so a database that came from the earlier 0008 shape
    # also accepts the current partial and incomplete terminal states.
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "DROP CONSTRAINT IF EXISTS ck_site_pages_ingestion_status"
        )
    )
    op.create_check_constraint(
        "ck_site_pages_ingestion_status",
        "site_pages",
        "ingestion_status IN ('pending', 'failed', 'partial', 'success')",
    )
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "DROP CONSTRAINT IF EXISTS ck_site_pages_announcement_ingestion_status"
        )
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
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS "
            "ix_site_pages_announcement_ingestion_status "
            "ON site_pages (announcement_ingestion_status)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_site_pages_host_crawl_lease_expires_at "
            "ON site_pages (host, crawl_lease_expires_at)"
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
    op.execute(
        sa.text(
            "ALTER TABLE site_pages "
            "DROP CONSTRAINT IF EXISTS ck_site_pages_announcement_ingestion_status"
        )
    )
    op.create_check_constraint(
        "ck_site_pages_announcement_ingestion_status",
        "site_pages",
        "announcement_ingestion_status IN "
        "('not_applicable', 'pending', 'failed', 'success')",
    )
    op.execute(
        sa.text("ALTER TABLE site_pages DROP COLUMN IF EXISTS ingestion_attempt_hash")
    )
