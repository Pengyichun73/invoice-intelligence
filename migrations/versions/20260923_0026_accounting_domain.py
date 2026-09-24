"""Add isolated accounting candidates, governed decisions, posting facts, and FX snapshots.

Revision ID: 20260923_0026_accounting
Revises: 20260923_0024
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0028_accounting"
down_revision: str | None = "20260923_0027_transaction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounting_candidates",
        sa.Column("candidate_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("tax_rule_version", sa.String(128), nullable=True),
        sa.Column("chart_of_accounts_version", sa.String(128), nullable=True),
        sa.Column("posting_rule_version", sa.String(128), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["extraction_runs.run_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_accounting_candidates_tenant_run"),
        sa.CheckConstraint(
            "status IN ('pending_rule_review', 'approved', 'rejected', 'posted')",
            name="ck_accounting_candidates_status",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_accounting_candidates_revision"),
    )
    op.create_index("ix_accounting_candidates_tenant_id", "accounting_candidates", ["tenant_id"])
    op.create_table(
        "tax_assessments",
        sa.Column("assessment_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False, unique=True),
        sa.Column("tax_rule_version", sa.String(128), nullable=False),
        sa.Column("tax_code", sa.String(128), nullable=False),
        sa.Column("taxable_amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("tax_amount", sa.Numeric(24, 8), nullable=False),
        sa.Column("advisory_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["accounting_candidates.candidate_id"], ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "posting_proposals",
        sa.Column("proposal_id", sa.String(64), primary_key=True),
        sa.Column("candidate_id", sa.String(64), nullable=False, unique=True),
        sa.Column("chart_of_accounts_version", sa.String(128), nullable=False),
        sa.Column("posting_rule_version", sa.String(128), nullable=False),
        sa.Column("lines_json", sa.JSON(), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["accounting_candidates.candidate_id"], ondelete="RESTRICT"
        ),
    )
    op.create_table(
        "exchange_rate_snapshots",
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("quote_currency", sa.String(16), nullable=False),
        sa.Column("base_currency", sa.String(16), nullable=False),
        sa.Column("rate", sa.Numeric(30, 12), nullable=False),
        sa.Column("precision", sa.Integer(), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_exchange_rate_snapshots_tenant_id", "exchange_rate_snapshots", ["tenant_id"]
    )
    op.create_table(
        "accounting_posting_attempts",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("external_reference", sa.String(256), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["accounting_candidates.candidate_id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_accounting_posting_attempts_tenant_id", "accounting_posting_attempts", ["tenant_id"]
    )
    op.create_table(
        "accounting_audits",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["accounting_candidates.candidate_id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("candidate_id", "revision", name="uq_accounting_audits_revision"),
    )
    op.create_index("ix_accounting_audits_tenant_id", "accounting_audits", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_accounting_audits_tenant_id", table_name="accounting_audits")
    op.drop_table("accounting_audits")
    op.drop_index(
        "ix_accounting_posting_attempts_tenant_id", table_name="accounting_posting_attempts"
    )
    op.drop_table("accounting_posting_attempts")
    op.drop_index("ix_exchange_rate_snapshots_tenant_id", table_name="exchange_rate_snapshots")
    op.drop_table("exchange_rate_snapshots")
    op.drop_table("posting_proposals")
    op.drop_table("tax_assessments")
    op.drop_index("ix_accounting_candidates_tenant_id", table_name="accounting_candidates")
    op.drop_table("accounting_candidates")
