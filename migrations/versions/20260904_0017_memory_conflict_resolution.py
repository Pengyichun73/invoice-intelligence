"""Add attributable conflict decisions and pending reevaluation registrations.

Revision ID: 20260904_0017
Revises: 20260903_0016
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0017"
down_revision: str | None = "20260903_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_conflict_resolution_decisions",
        sa.Column("resolution_decision_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "conflict_id",
            sa.String(length=64),
            sa.ForeignKey("memory_conflicts.conflict_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("previous_status", sa.String(length=32), nullable=False),
        sa.Column("target_status", sa.String(length=32), nullable=False),
        sa.Column("selected_canonical_field_path", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_memory_conflict_resolution_decisions_idempotency",
        ),
        sa.CheckConstraint(
            "previous_status = 'open'",
            name="ck_memory_conflict_resolution_decisions_previous_status",
        ),
        sa.CheckConstraint(
            "target_status IN ('resolved', 'dismissed')",
            name="ck_memory_conflict_resolution_decisions_target_status",
        ),
    )
    op.create_index(
        "ix_memory_conflict_resolution_decisions_tenant_id",
        "memory_conflict_resolution_decisions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_conflict_resolution_decisions_conflict_id",
        "memory_conflict_resolution_decisions",
        ["conflict_id"],
    )
    op.create_index(
        "ix_memory_conflict_resolution_decisions_conflict",
        "memory_conflict_resolution_decisions",
        ["tenant_id", "conflict_id", "decided_at"],
    )

    op.create_table(
        "memory_conflict_reevaluation_requests",
        sa.Column("reevaluation_request_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column(
            "resolution_decision_id",
            sa.String(length=64),
            sa.ForeignKey(
                "memory_conflict_resolution_decisions.resolution_decision_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "resolution_decision_id",
            "target_type",
            "target_id",
            name="uq_memory_conflict_reevaluation_requests_target",
        ),
        sa.CheckConstraint(
            "target_type IN ('memory_admission', 'field_alias')",
            name="ck_memory_conflict_reevaluation_requests_target_type",
        ),
        sa.CheckConstraint(
            "status = 'pending'",
            name="ck_memory_conflict_reevaluation_requests_status",
        ),
    )
    op.create_index(
        "ix_memory_conflict_reevaluation_requests_tenant_id",
        "memory_conflict_reevaluation_requests",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_conflict_reevaluation_requests_queue",
        "memory_conflict_reevaluation_requests",
        ["tenant_id", "status", "target_type", "target_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_conflict_reevaluation_requests_queue",
        table_name="memory_conflict_reevaluation_requests",
    )
    op.drop_index(
        "ix_memory_conflict_reevaluation_requests_tenant_id",
        table_name="memory_conflict_reevaluation_requests",
    )
    op.drop_table("memory_conflict_reevaluation_requests")
    op.drop_index(
        "ix_memory_conflict_resolution_decisions_conflict",
        table_name="memory_conflict_resolution_decisions",
    )
    op.drop_index(
        "ix_memory_conflict_resolution_decisions_conflict_id",
        table_name="memory_conflict_resolution_decisions",
    )
    op.drop_index(
        "ix_memory_conflict_resolution_decisions_tenant_id",
        table_name="memory_conflict_resolution_decisions",
    )
    op.drop_table("memory_conflict_resolution_decisions")
