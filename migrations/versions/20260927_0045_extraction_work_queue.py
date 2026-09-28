"""Persist invoice workflow start and review-resume requests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0045_extraction_work_queue"
down_revision: str | None = "20260924_0044_code_harness_repair_route"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "extraction_work_items",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column(
            "run_id", sa.String(64),
            sa.ForeignKey("extraction_runs.run_id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("correction_json", sa.JSON(), nullable=True),
        sa.Column("idempotency_hash", sa.String(64), nullable=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("checkpoint_id", sa.String(256), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "kind", "idempotency_hash", name="uq_extraction_work_idempotency"
        ),
    )
    op.create_index(
        "ix_extraction_work_claim",
        "extraction_work_items",
        ["status", "next_attempt_at", "lease_expires_at", "created_at"],
    )
    op.create_index(
        "ix_extraction_work_run_kind_status",
        "extraction_work_items",
        ["run_id", "kind", "status"],
    )
    op.create_index("ix_extraction_work_items_tenant_id", "extraction_work_items", ["tenant_id"])
    op.create_index("ix_extraction_work_items_status", "extraction_work_items", ["status"])


def downgrade() -> None:
    op.drop_index("ix_extraction_work_run_kind_status", table_name="extraction_work_items")
    op.drop_index("ix_extraction_work_items_status", table_name="extraction_work_items")
    op.drop_index("ix_extraction_work_items_tenant_id", table_name="extraction_work_items")
    op.drop_index("ix_extraction_work_claim", table_name="extraction_work_items")
    op.drop_table("extraction_work_items")
