"""Add tenant-scoped transaction analysis facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0027_transaction"
down_revision: str | None = "20260923_0026_promotion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transaction_candidates",
        sa.Column("candidate_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("document_id", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_transaction_candidate_run"),
    )
    op.create_index("ix_transaction_candidates_tenant_id", "transaction_candidates", ["tenant_id"])
    op.create_index("ix_transaction_candidates_run_id", "transaction_candidates", ["run_id"])
    op.create_index(
        "ix_transaction_candidates_document_id", "transaction_candidates", ["document_id"]
    )
    op.create_index(
        "ix_transaction_candidate_fingerprint",
        "transaction_candidates",
        ["tenant_id", "fingerprint"],
    )
    op.create_table(
        "transaction_analyses",
        sa.Column("analysis_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("classification_json", sa.JSON(), nullable=False),
        sa.Column("duplicate_json", sa.JSON(), nullable=True),
        sa.Column("suspicious_json", sa.JSON(), nullable=True),
        sa.Column("assessment_json", sa.JSON(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["transaction_candidates.candidate_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("tenant_id", "candidate_id", name="uq_transaction_analysis_candidate"),
    )
    op.create_index("ix_transaction_analyses_tenant_id", "transaction_analyses", ["tenant_id"])
    op.create_index(
        "ix_transaction_analyses_candidate_id", "transaction_analyses", ["candidate_id"]
    )
    op.create_table(
        "transaction_analysis_audits",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key_hash", name="uq_transaction_audit_idempotency"
        ),
    )
    op.create_index(
        "ix_transaction_analysis_audits_tenant_id", "transaction_analysis_audits", ["tenant_id"]
    )
    op.create_index(
        "ix_transaction_analysis_audits_candidate_id",
        "transaction_analysis_audits",
        ["candidate_id"],
    )


def downgrade() -> None:
    op.drop_table("transaction_analysis_audits")
    op.drop_table("transaction_analyses")
    op.drop_table("transaction_candidates")
