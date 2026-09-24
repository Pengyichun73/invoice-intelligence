"""Isolated snapshot, queue, schedule, aggregate report and artifact facts.

Revision ID: 20260923_0035_evaluation
Revises: 20260923_0034_index_lease
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0035_evaluation"
down_revision: str | None = "20260923_0034_index_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_snapshots",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("cases_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "dataset_version", name="uq_evaluation_snapshot_version"),
    )
    op.create_index("ix_evaluation_snapshots_tenant_id", "evaluation_snapshots", ["tenant_id"])
    op.create_table(
        "evaluation_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("evaluation_snapshots.snapshot_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("dataset_version", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("index_version", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(256), nullable=False),
        sa.Column("prompt_version", sa.String(256), nullable=False),
        sa.Column("threshold_version", sa.String(128), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("failure_code", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key_hash", name="uq_evaluation_job_idempotency"),
        sa.CheckConstraint("status IN ('pending', 'running', 'completed', 'failed', 'quarantined')", name="ck_evaluation_job_status"),
        sa.CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND worker_id IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)", name="ck_evaluation_job_lease",
        ),
    )
    op.create_index("ix_evaluation_jobs_tenant_id", "evaluation_jobs", ["tenant_id"])
    op.create_index("ix_evaluation_jobs_queue", "evaluation_jobs", ["status", "next_attempt_at", "lease_expires_at"])
    op.create_table(
        "evaluation_schedules",
        sa.Column("schedule_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("evaluation_snapshots.snapshot_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("index_version", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(256), nullable=False),
        sa.Column("prompt_version", sa.String(256), nullable=False),
        sa.Column("threshold_version", sa.String(128), nullable=False),
        sa.Column("interval_seconds", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.CheckConstraint("interval_seconds >= 3600", name="ck_evaluation_schedule_interval"),
    )
    op.create_index("ix_evaluation_schedules_tenant_id", "evaluation_schedules", ["tenant_id"])
    op.create_index("ix_evaluation_schedules_due", "evaluation_schedules", ["enabled", "next_run_at"])
    op.create_table(
        "evaluation_job_reports",
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("evaluation_jobs.job_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evaluation_job_reports_tenant_id", "evaluation_job_reports", ["tenant_id"])
    op.create_table(
        "evaluation_job_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("evaluation_jobs.job_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("reference", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evaluation_job_artifacts_tenant_id", "evaluation_job_artifacts", ["tenant_id"])


def downgrade() -> None:
    for table in (
        "evaluation_job_artifacts", "evaluation_job_reports", "evaluation_schedules",
        "evaluation_jobs", "evaluation_snapshots",
    ):
        op.drop_table(table)
