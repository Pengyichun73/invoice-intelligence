"""Bind promotion candidates to trusted model evidence.

Revision ID: 20260923_0033_promotion
Revises: 20260923_0032_transaction
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0033_promotion"
down_revision: str | None = "20260923_0032_transaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "promotion_candidates",
        sa.Column("artifact_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "promotion_candidates",
        sa.Column("model_evaluation_id", sa.String(128), nullable=True),
    )
    op.create_check_constraint(
        "ck_promotion_candidates_evidence_pair",
        "promotion_candidates",
        "(artifact_id IS NULL AND model_evaluation_id IS NULL) OR "
        "(artifact_id IS NOT NULL AND model_evaluation_id IS NOT NULL)",
    )
    op.create_index(
        "uq_promotion_candidates_tenant_active",
        "promotion_candidates",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_promotion_candidates_tenant_active", table_name="promotion_candidates")
    op.drop_constraint("ck_promotion_candidates_evidence_pair", "promotion_candidates")
    op.drop_column("promotion_candidates", "model_evaluation_id")
    op.drop_column("promotion_candidates", "artifact_id")
