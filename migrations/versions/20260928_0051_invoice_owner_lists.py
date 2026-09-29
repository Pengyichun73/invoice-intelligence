"""Record trusted creator identity for personal extraction lists."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0051_invoice_owner_lists"
down_revision: str | None = "20260928_0050_invoice_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("extraction_runs", sa.Column("created_by", sa.String(128), nullable=True))
    op.add_column("invoice_batches", sa.Column("created_by", sa.String(128), nullable=True))
    op.create_index("ix_extraction_runs_owner_recent", "extraction_runs", ["tenant_id", "created_by", "created_at"])
    op.create_index("ix_invoice_batches_owner_recent", "invoice_batches", ["tenant_id", "created_by", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_invoice_batches_owner_recent", table_name="invoice_batches")
    op.drop_index("ix_extraction_runs_owner_recent", table_name="extraction_runs")
    op.drop_column("invoice_batches", "created_by")
    op.drop_column("extraction_runs", "created_by")
