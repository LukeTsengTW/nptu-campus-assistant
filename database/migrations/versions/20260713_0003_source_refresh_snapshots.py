"""新增公告來源最後成功抓取快照。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260713_0003"
down_revision: str | None = "20260712_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column(
            "canonical_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "sources",
        sa.Column("last_successful_crawl_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("sources", "last_successful_crawl_at")
    op.drop_column("sources", "canonical_urls")
