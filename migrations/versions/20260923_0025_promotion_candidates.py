"""Add independent deterministic promotion candidate facts and audit state."""

from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0026_promotion"
down_revision: str | None = "20260923_0025_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "promotion_candidates",
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("dataset_version", sa.String(128), nullable=False),
        sa.Column("evaluation_run_id", sa.String(128), nullable=False),
        sa.Column("model_version", sa.String(256), nullable=False),
        sa.Column("prompt_version", sa.String(256), nullable=False),
        sa.Column("schema_version", sa.String(64), nullable=False),
        sa.Column("index_version", sa.String(256), nullable=False),
        sa.Column("threshold_version", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("metric_json", sa.JSON(), nullable=False),
        sa.Column("hard_failure_code", sa.String(128), nullable=True),
        sa.Column("compatibility_errors_json", sa.JSON(), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_promotion_candidates"),
        sa.UniqueConstraint("tenant_id", "candidate_id", name="uq_promotion_candidates_tenant_id"),
        sa.CheckConstraint(
            "status IN ('shadow', 'canary', 'active', 'rollback', 'rejected')",
            name="ck_promotion_candidates_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_promotion_candidates_revision"),
    )
    op.create_index(
        "ix_promotion_candidates_scope_status",
        "promotion_candidates",
        ["tenant_id", "status", "updated_at"],
    )
    op.create_table(
        "promotion_candidate_audits",
        sa.Column("audit_id", sa.String(128), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(128), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=False),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("audit_id", name="pk_promotion_candidate_audits"),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["promotion_candidates.candidate_id"],
            name="fk_promotion_candidate_audits_candidate", ondelete="RESTRICT",
        ),
    )


def downgrade() -> None:
    op.drop_table("promotion_candidate_audits")
    op.drop_index("ix_promotion_candidates_scope_status", table_name="promotion_candidates")
    op.drop_table("promotion_candidates")
