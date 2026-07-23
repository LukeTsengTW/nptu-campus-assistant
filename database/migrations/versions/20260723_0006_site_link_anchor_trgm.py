"""add trigram index for site link anchors

Revision ID: 20260723_0006
Revises: 20260723_0005
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260723_0006"
down_revision: str | None = "20260723_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_site_links_anchor_text_trgm",
        "site_links",
        ["anchor_text"],
        postgresql_using="gin",
        postgresql_ops={"anchor_text": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_site_links_anchor_text_trgm", table_name="site_links")
