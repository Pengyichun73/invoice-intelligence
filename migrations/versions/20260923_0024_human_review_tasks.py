"""Add production human review task lifecycle and audit facts.

Revision ID: 20260923_0024
Revises: 20260923_0023
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0024"
down_revision: str | None = "20260923_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("review_tasks") as batch:
        batch.add_column(sa.Column("tenant_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("priority", sa.Integer(), nullable=False, server_default="50"))
        batch.add_column(sa.Column("assigned_reviewer_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("cancel_reason", sa.String(512), nullable=True))
    op.execute(
        "UPDATE review_tasks SET tenant_id = "
        "(SELECT tenant_id FROM extraction_runs WHERE extraction_runs.run_id = review_tasks.run_id)"
    )
    op.execute("UPDATE review_tasks SET status = 'pending_review' WHERE status = 'pending'")
    op.execute(
        "UPDATE review_tasks SET status = 'submitted', submitted_at = resolved_at "
        "WHERE status = 'completed'"
    )
    with op.batch_alter_table("review_tasks") as batch:
        batch.alter_column("tenant_id", existing_type=sa.String(128), nullable=False)
        batch.create_check_constraint(
            "ck_review_tasks_status_v2",
            "status IN ('pending_review', 'claimed', 'submitted', 'expired', 'cancelled')",
        )
        batch.create_check_constraint("ck_review_tasks_priority", "priority BETWEEN 0 AND 100")
        batch.create_check_constraint("ck_review_tasks_revision", "revision >= 1")
        batch.create_check_constraint(
            "ck_review_tasks_lease_v2",
            "(status = 'claimed' AND assigned_reviewer_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_token IS NULL AND lease_expires_at IS NULL)",
        )
        batch.create_index(
            "ix_review_tasks_tenant_queue",
            ["tenant_id", "status", "priority", "created_at", "review_id"],
        )
        batch.create_index(
            "ix_review_tasks_tenant_reviewer",
            ["tenant_id", "assigned_reviewer_id", "status"],
        )

    op.create_table(
        "review_task_audits",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("review_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("target_reviewer_id", sa.String(128), nullable=True),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["review_id"], ["review_tasks.review_id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "tenant_id", "review_id", "revision", name="uq_review_task_audits_revision"
        ),
    )
    op.create_index(
        "ix_review_task_audits_tenant_task_time",
        "review_task_audits",
        ["tenant_id", "review_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_review_task_audits_tenant_task_time", table_name="review_task_audits")
    op.drop_table("review_task_audits")
    with op.batch_alter_table("review_tasks") as batch:
        batch.drop_index("ix_review_tasks_tenant_reviewer")
        batch.drop_index("ix_review_tasks_tenant_queue")
        batch.drop_constraint("ck_review_tasks_lease_v2", type_="check")
        batch.drop_constraint("ck_review_tasks_revision", type_="check")
        batch.drop_constraint("ck_review_tasks_priority", type_="check")
        batch.drop_constraint("ck_review_tasks_status_v2", type_="check")
    op.execute(
        "UPDATE review_tasks SET status = 'pending' "
        "WHERE status IN ('pending_review', 'claimed', 'expired')"
    )
    op.execute(
        "UPDATE review_tasks SET status = 'completed' WHERE status IN ('submitted', 'cancelled')"
    )
    with op.batch_alter_table("review_tasks") as batch:
        batch.drop_column("cancel_reason")
        batch.drop_column("cancelled_at")
        batch.drop_column("submitted_at")
        batch.drop_column("revision")
        batch.drop_column("lease_expires_at")
        batch.drop_column("lease_token")
        batch.drop_column("assigned_reviewer_id")
        batch.drop_column("priority")
        batch.drop_column("tenant_id")
