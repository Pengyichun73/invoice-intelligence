"""Persist the next route and retry reason for repair-pending tasks."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0044_code_harness_repair_route"
down_revision: str | None = "20260924_0043_code_harness_postmortem_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("code_harness_tasks") as batch_op:
        batch_op.add_column(sa.Column("next_stage", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("retry_reason_code", sa.String(64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("code_harness_tasks") as batch_op:
        batch_op.drop_column("retry_reason_code")
        batch_op.drop_column("next_stage")
