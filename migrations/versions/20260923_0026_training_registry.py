"""Add asynchronous Training Job, dataset export, artifact, and audit registries.

Revision ID: 20260923_0029_training
Revises: 20260923_0028_accounting
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0029_training"
down_revision: str | None = "20260923_0028_accounting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_dataset_exports",
        sa.Column("export_id", sa.String(64), primary_key=True),
        sa.Column("dataset_key", sa.String(64), nullable=False, unique=True),
        sa.Column("tenant_scope", sa.String(128), nullable=False),
        sa.Column("dataset_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(128), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("manifest_uri", sa.String(1024), nullable=False),
        sa.Column("manifest_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("positive_count", sa.Integer(), nullable=False),
        sa.Column("hard_negative_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("export_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_key"], ["training_dataset_versions.dataset_key"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "tenant_scope", "dataset_id", "dataset_version",
            name="uq_training_dataset_exports_identity",
        ),
        sa.CheckConstraint("record_count > 0", name="ck_training_dataset_exports_records"),
        sa.CheckConstraint("positive_count > 0", name="ck_training_dataset_exports_positive"),
        sa.CheckConstraint(
            "hard_negative_count >= 0", name="ck_training_dataset_exports_negatives"
        ),
    )
    op.create_index(
        "ix_training_dataset_exports_tenant_scope",
        "training_dataset_exports",
        ["tenant_scope"],
    )

    op.create_table(
        "training_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("dataset_export_id", sa.String(64), nullable=False),
        sa.Column("training_run_key", sa.String(64), nullable=True),
        sa.Column("training_run_id", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("remote_job_id", sa.String(256), nullable=True),
        sa.Column("retry_of_job_id", sa.String(64), nullable=True),
        sa.Column("idempotency_digest", sa.String(64), nullable=False),
        sa.Column("operation_submit_id", sa.String(96), nullable=False, unique=True),
        sa.Column("operation_cancel_id", sa.String(96), nullable=False, unique=True),
        sa.Column("operation_commit_id", sa.String(96), nullable=False, unique=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("claim_count", sa.Integer(), nullable=False),
        sa.Column("failure_attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_by", sa.String(128), nullable=True),
        sa.Column("cancel_reason", sa.String(512), nullable=True),
        sa.Column("job_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_export_id"], ["training_dataset_exports.export_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["training_run_key"], ["training_runs.training_run_key"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_job_id"], ["training_jobs.job_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", "training_run_id", name="uq_training_jobs_run"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_digest", name="uq_training_jobs_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('planned','submitted','running','succeeded','failed',"
            "'cancelled','quarantined')",
            name="ck_training_jobs_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_training_jobs_revision"),
        sa.CheckConstraint(
            "claim_count >= 0 AND failure_attempt_count >= 0",
            name="ck_training_jobs_attempts",
        ),
    )
    op.create_index("ix_training_jobs_tenant_id", "training_jobs", ["tenant_id"])
    op.create_index(
        "ix_training_jobs_worker_queue",
        "training_jobs",
        ["status", "next_attempt_at", "lease_expires_at", "created_at"],
    )

    op.create_table(
        "training_artifacts",
        sa.Column("artifact_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False, unique=True),
        sa.Column("training_run_id", sa.String(128), nullable=False),
        sa.Column("remote_job_id", sa.String(256), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False),
        sa.Column("source_uri", sa.String(1024), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("artifact_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["training_jobs.job_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "tenant_id", "checksum_sha256", name="uq_training_artifacts_checksum"
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_training_artifacts_size"),
    )
    op.create_index("ix_training_artifacts_tenant_id", "training_artifacts", ["tenant_id"])

    op.create_table(
        "training_audit_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["training_jobs.job_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("job_id", "revision", name="uq_training_audit_job_revision"),
    )
    op.create_index(
        "ix_training_audit_events_tenant_id", "training_audit_events", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_training_audit_events_tenant_id", table_name="training_audit_events")
    op.drop_table("training_audit_events")
    op.drop_index("ix_training_artifacts_tenant_id", table_name="training_artifacts")
    op.drop_table("training_artifacts")
    op.drop_index("ix_training_jobs_worker_queue", table_name="training_jobs")
    op.drop_index("ix_training_jobs_tenant_id", table_name="training_jobs")
    op.drop_table("training_jobs")
    op.drop_index(
        "ix_training_dataset_exports_tenant_scope", table_name="training_dataset_exports"
    )
    op.drop_table("training_dataset_exports")
