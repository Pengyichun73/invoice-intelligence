"""Add transaction assessment CAS revision and replayable audit snapshot.

Revision ID: 20260923_0032_transaction
Revises: 20260923_0031_storage_fk
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0032_transaction"
down_revision: str | None = "20260923_0031_storage_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transaction_analyses",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "transaction_analysis_audits",
        sa.Column("assessment_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transaction_analysis_audits", "assessment_json")
    op.drop_column("transaction_analyses", "revision")
