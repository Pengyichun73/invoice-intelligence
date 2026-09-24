"""Persist Code Generation and Self-Healing Harness facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0040_code_harness"
down_revision: str | None = "20260924_0039_evaluation_report_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "code_harness_tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(128)),
        sa.Column("repository_id", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("versions_json", sa.JSON, nullable=False),
        sa.Column("budget_json", sa.JSON, nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_token", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key_hash", name="uq_code_harness_task_idempotency"),
    )
    op.create_index(
        "ix_code_harness_tasks_scope", "code_harness_tasks",
        ["tenant_id", "repository_id", "status"],
    )
    op.create_table(
        "code_harness_attempts",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("code_harness_tasks.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("lease_token", sa.String(128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_id", "attempt_number", name="uq_code_harness_attempt_number"),
    )
    op.create_table(
        "code_harness_snapshots",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("repository_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("source_revision", sa.String(256), nullable=False),
        sa.Column("manifest_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("files_json", sa.JSON, nullable=False),
        sa.Column("versions_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "repository_id", "revision", name="uq_code_harness_snapshot_revision"),
    )
    op.create_index("ix_code_harness_snapshots_tenant_id", "code_harness_snapshots", ["tenant_id"])
    op.create_table(
        "code_harness_patches",
        sa.Column("patch_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("repository_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("snapshot_revision", sa.Integer, nullable=False),
        sa.Column("operations_json", sa.JSON, nullable=False),
        sa.Column("patch_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_code_harness_patches_tenant_id", "code_harness_patches", ["tenant_id"])
    op.create_table(
        "code_harness_executions",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("code_harness_tasks.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_summary", sa.String(4096), nullable=False),
        sa.Column("error_signature", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", name="uq_code_harness_execution_task"),
    )
    op.create_table(
        "code_harness_watchdogs",
        sa.Column("observation_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("error_signature", sa.String(128)),
        sa.Column("changed_ast_fingerprint", sa.Boolean, nullable=False),
        sa.Column("changed_symbols", sa.Boolean, nullable=False),
        sa.Column("changed_diagnostics", sa.Boolean, nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_code_harness_watchdogs_task_id", "code_harness_watchdogs", ["task_id"])
    op.create_table(
        "code_harness_postmortems",
        sa.Column("postmortem_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("version_scope", sa.String(256), nullable=False),
        sa.Column("occurrence_count", sa.Integer, nullable=False),
        sa.Column("admission_status", sa.String(32), nullable=False),
        sa.Column("error_signature", sa.String(128)),
        sa.Column("root_cause", sa.String(4096)),
        sa.Column("solution_pattern", sa.String(4096)),
        sa.Column("affected_language", sa.String(64)),
        sa.Column("affected_symbol_kind", sa.String(64)),
        sa.Column("patch_shape", sa.String(128)),
        sa.Column("source_trace_id", sa.String(128)),
        sa.Column("source_event_ids_json", sa.JSON, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "fingerprint", "version_scope", name="uq_code_harness_postmortem_scope"),
    )
    op.create_index("ix_code_harness_postmortems_tenant_id", "code_harness_postmortems", ["tenant_id"])
    op.create_table(
        "code_harness_postmortem_source_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("postmortem_id", sa.String(64), nullable=False),
        sa.Column("fingerprint", sa.String(128), nullable=False),
        sa.Column("version_scope", sa.String(256), nullable=False),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("payload_summary", sa.String(512), nullable=False),
        sa.Column("payload_checksum_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_code_harness_source_events_tenant_id", "code_harness_postmortem_source_events", ["tenant_id"])
    op.create_table(
        "code_harness_audits",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_code_harness_audits_tenant_id", "code_harness_audits", ["tenant_id"])
    op.create_table(
        "code_harness_projections",
        sa.Column("projection_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("index_version", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("lease_token", sa.String(128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "snapshot_id", "index_version", name="uq_code_harness_projection"),
    )
    op.create_index("ix_code_harness_projections_tenant_id", "code_harness_projections", ["tenant_id"])


def downgrade() -> None:
    for table in (
        "code_harness_projections",
        "code_harness_audits",
        "code_harness_postmortem_source_events",
        "code_harness_postmortems",
        "code_harness_watchdogs",
        "code_harness_executions",
        "code_harness_patches",
        "code_harness_snapshots",
        "code_harness_attempts",
        "code_harness_tasks",
    ):
        op.drop_table(table)
