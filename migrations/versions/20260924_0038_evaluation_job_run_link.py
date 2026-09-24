"""Fence Suite promotion evidence behind a completed evaluation Job.

Revision ID: 20260924_0038_evaluation_job_run_link
Revises: 20260924_0037_evaluation_suite_jobs
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0038_evaluation_job_run_link"
down_revision: str | None = "20260924_0037_evaluation_suite_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_jobs") as batch:
        batch.add_column(sa.Column("evaluation_run_id", sa.String(128), nullable=True))
        batch.create_unique_constraint(
            "uq_evaluation_jobs_run_id", ["evaluation_run_id"]
        )
        batch.create_check_constraint(
            "ck_evaluation_job_run_link",
            "(evidence_class = 'diagnostic_only' AND evaluation_run_id IS NULL) OR "
            "(evidence_class = 'suite_run' AND "
            "((status = 'completed' AND evaluation_run_id IS NOT NULL) OR "
            "(status <> 'completed' AND evaluation_run_id IS NULL)))",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text(
        "SELECT count(*) FROM evaluation_jobs WHERE evaluation_run_id IS NOT NULL"
    )):
        raise RuntimeError("Completed Suite evaluation links must be retained before downgrade")
    with op.batch_alter_table("evaluation_jobs") as batch:
        batch.drop_constraint("ck_evaluation_job_run_link", type_="check")
        batch.drop_constraint("uq_evaluation_jobs_run_id", type_="unique")
        batch.drop_column("evaluation_run_id")
