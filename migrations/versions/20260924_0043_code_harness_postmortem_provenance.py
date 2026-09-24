"""Persist immutable Harness Postmortem source provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0043_code_harness_postmortem_provenance"
down_revision: str | None = "20260924_0042_code_harness_execution_write_once"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("code_harness_postmortem_source_events") as batch_op:
        for name, length in (
            ("source_repository_id", 128),
            ("source_snapshot_id", 64),
            ("source_revision", 256),
            ("source_task_id", 64),
            ("source_patch_id", 64),
            ("source_execution_id", 64),
        ):
            batch_op.add_column(sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("code_harness_postmortem_source_events") as batch_op:
        for name in (
            "source_execution_id",
            "source_patch_id",
            "source_task_id",
            "source_revision",
            "source_snapshot_id",
            "source_repository_id",
        ):
            batch_op.drop_column(name)
