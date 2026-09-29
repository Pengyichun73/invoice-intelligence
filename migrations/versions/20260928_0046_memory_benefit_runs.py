"""Store only version-bound, value-free paired memory judgments."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0046_memory_benefit_runs"
down_revision: str | None = "20260927_0045_extraction_work_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_benefit_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dataset_digest", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("catalog_version", sa.String(128), nullable=False),
        sa.Column("index_version", sa.String(256), nullable=False),
        sa.Column("model_version", sa.String(256), nullable=False),
        sa.Column("prompt_version", sa.String(256), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("template_group_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("scenarios_json", sa.JSON(), nullable=False),
        sa.Column("blocker_codes_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed')",
            name="ck_memory_benefit_runs_status",
        ),
        sa.CheckConstraint(
            "case_count >= 0 AND template_group_count >= 0",
            name="ck_memory_benefit_runs_counts",
        ),
    )
    op.create_index("ix_memory_benefit_runs_tenant_id", "memory_benefit_runs", ["tenant_id"])
    op.create_index(
        "ix_memory_benefit_runs_tenant_created",
        "memory_benefit_runs", ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_benefit_runs_tenant_created", table_name="memory_benefit_runs")
    op.drop_index("ix_memory_benefit_runs_tenant_id", table_name="memory_benefit_runs")
    op.drop_table("memory_benefit_runs")
