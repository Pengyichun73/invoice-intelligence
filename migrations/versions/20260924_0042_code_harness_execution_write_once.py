"""Allow one immutable execution fact per Harness attempt."""

from collections.abc import Sequence

from alembic import op

revision: str = "20260924_0042_code_harness_execution_write_once"
down_revision: str | None = "20260924_0041_code_harness_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_code_harness_execution_task",
        "code_harness_executions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_code_harness_execution_attempt",
        "code_harness_executions",
        ["task_id", "attempt_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_code_harness_execution_attempt",
        "code_harness_executions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_code_harness_execution_task",
        "code_harness_executions",
        ["task_id"],
    )
