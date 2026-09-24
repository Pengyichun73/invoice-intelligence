"""Persist versioned aggregate evaluation reports for integrity gates.

Revision ID: 20260924_0039_evaluation_report_artifacts
Revises: 20260924_0038_evaluation_job_run_link
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0039_evaluation_report_artifacts"
down_revision: str | None = "20260924_0038_evaluation_job_run_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_report_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("evaluation_run_key", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("report_kind", sa.String(16), nullable=False),
        sa.Column("report_schema_version", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["evaluation_run_key"], ["evaluation_runs.evaluation_run_key"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "evaluation_run_key", "report_kind",
            name="uq_evaluation_report_run_kind",
        ),
        sa.CheckConstraint(
            "report_kind IN ('json', 'markdown')",
            name="ck_evaluation_report_kind",
        ),
    )
    op.create_index(
        "ix_evaluation_report_tenant_run", "evaluation_report_artifacts",
        ["tenant_id", "evaluation_run_key"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT count(*) FROM evaluation_report_artifacts")):
        raise RuntimeError("Evaluation report artifacts must be retained before downgrade")
    op.drop_index("ix_evaluation_report_tenant_run", table_name="evaluation_report_artifacts")
    op.drop_table("evaluation_report_artifacts")
