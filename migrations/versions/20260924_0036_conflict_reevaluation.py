"""Track conflict reevaluation consumption without rewriting review facts.

Revision ID: 20260924_0036_conflict_reevaluation
Revises: 20260923_0035_evaluation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0036_conflict_reevaluation"
down_revision: str | None = "20260923_0035_evaluation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")
    op.add_column(
        "memory_conflict_reevaluation_requests",
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.drop_constraint(
        "ck_memory_conflict_reevaluation_requests_status",
        "memory_conflict_reevaluation_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_conflict_reevaluation_requests_status",
        "memory_conflict_reevaluation_requests",
        "status IN ('pending', 'completed', 'requires_review', 'invalid_target')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE memory_conflict_reevaluation_requests SET status = 'pending', "
        "completed_at = NULL WHERE status <> 'pending'"
    )
    op.drop_constraint(
        "ck_memory_conflict_reevaluation_requests_status",
        "memory_conflict_reevaluation_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_memory_conflict_reevaluation_requests_status",
        "memory_conflict_reevaluation_requests",
        "status = 'pending'",
    )
    op.drop_column("memory_conflict_reevaluation_requests", "completed_at")
