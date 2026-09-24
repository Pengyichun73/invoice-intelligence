"""Add replay-safe recovery outbox for reviewed-example materialization.

Revision ID: 20260903_0016
Revises: 20260903_0015
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260903_0016"
down_revision: str | None = "20260903_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_review_recoveries",
        sa.Column("recovery_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.String(length=36),
            sa.ForeignKey("documents.document_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "correction_id",
            sa.String(length=64),
            sa.ForeignKey("human_corrections.correction_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("correction_event_ids_json", sa.JSON(), nullable=False),
        sa.Column("original_result_json", sa.JSON(), nullable=False),
        sa.Column("reviewed_result_json", sa.JSON(), nullable=False),
        sa.Column("example_ids_json", sa.JSON(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'failed_retryable', 'completed')",
            name="ck_memory_review_recoveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_memory_review_recoveries_attempt_count",
        ),
        sa.CheckConstraint(
            "(worker_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_memory_review_recoveries_lease",
        ),
        sa.CheckConstraint(
            "(last_error_code IS NULL AND last_error_at IS NULL) "
            "OR (last_error_code IS NOT NULL AND last_error_at IS NOT NULL)",
            name="ck_memory_review_recoveries_last_error",
        ),
        sa.UniqueConstraint(
            "correction_id", name="uq_memory_review_recoveries_correction_id"
        ),
        sa.UniqueConstraint("trace_id", name="uq_memory_review_recoveries_trace_id"),
    )
    op.create_index(
        "ix_memory_review_recoveries_tenant_id",
        "memory_review_recoveries",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_review_recoveries_worker_queue",
        "memory_review_recoveries",
        ["status", "next_attempt_at", "lease_expires_at", "recovery_id"],
    )
    op.create_index(
        "ix_memory_review_recoveries_tenant_run",
        "memory_review_recoveries",
        ["tenant_id", "run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_review_recoveries_tenant_run",
        table_name="memory_review_recoveries",
    )
    op.drop_index(
        "ix_memory_review_recoveries_worker_queue",
        table_name="memory_review_recoveries",
    )
    op.drop_index(
        "ix_memory_review_recoveries_tenant_id",
        table_name="memory_review_recoveries",
    )
    op.drop_table("memory_review_recoveries")
