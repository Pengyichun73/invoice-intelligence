"""Add tenant-scoped offline evaluation datasets and runs.

Revision ID: 20260901_0005
Revises: 20260901_0004
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0005"
down_revision: str | None = "20260901_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_datasets",
        sa.Column("dataset_key", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("dataset_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("dataset_key", name="pk_evaluation_datasets"),
        sa.UniqueConstraint(
            "tenant_id",
            "dataset_id",
            "dataset_version",
            name="uq_evaluation_datasets_tenant_identity",
        ),
    )
    op.create_index(
        "ix_evaluation_datasets_tenant_id",
        "evaluation_datasets",
        ["tenant_id"],
    )
    op.create_index(
        "ix_evaluation_datasets_tenant_schema",
        "evaluation_datasets",
        ["tenant_id", "schema_version"],
    )

    op.create_table(
        "evaluation_runs",
        sa.Column("evaluation_run_key", sa.String(length=64), nullable=False),
        sa.Column("evaluation_run_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_key", sa.String(length=64), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("index_version", sa.String(length=256), nullable=False),
        sa.Column("model_version", sa.String(length=256), nullable=False),
        sa.Column("prompt_version", sa.String(length=256), nullable=False),
        sa.Column("retrieval_policy_version", sa.String(length=256), nullable=False),
        sa.Column("threshold_version", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('created', 'running', 'completed', 'failed')",
            name="ck_evaluation_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["evaluation_datasets.dataset_key"],
            name="fk_evaluation_runs_dataset_key_evaluation_datasets",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("evaluation_run_key", name="pk_evaluation_runs"),
        sa.UniqueConstraint(
            "tenant_id",
            "evaluation_run_id",
            name="uq_evaluation_runs_tenant_identity",
        ),
    )
    op.create_index(
        "ix_evaluation_runs_tenant_id",
        "evaluation_runs",
        ["tenant_id"],
    )
    op.create_index(
        "ix_evaluation_runs_tenant_dataset",
        "evaluation_runs",
        ["tenant_id", "dataset_id", "dataset_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_runs_tenant_dataset", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_tenant_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index(
        "ix_evaluation_datasets_tenant_schema",
        table_name="evaluation_datasets",
    )
    op.drop_index(
        "ix_evaluation_datasets_tenant_id",
        table_name="evaluation_datasets",
    )
    op.drop_table("evaluation_datasets")
