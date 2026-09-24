"""Add tenant-scoped memory governance audit and retrieval telemetry.

Revision ID: 20260901_0008
Revises: 20260901_0007
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0008"
down_revision: str | None = "20260901_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_governance_audits",
        sa.Column("audit_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=256), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('disable_example', 'invalidate_schema', "
            "'rebuild_index', 'submit_feedback')",
            name="ck_memory_governance_audits_action",
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_memory_governance_audits"),
        sa.UniqueConstraint(
            "tenant_id",
            "action",
            "idempotency_key_hash",
            name="uq_memory_governance_audits_idempotency",
        ),
    )
    op.create_index(
        "ix_memory_governance_audits_tenant_id",
        "memory_governance_audits",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_governance_audits_resource",
        "memory_governance_audits",
        ["tenant_id", "resource_type", "resource_id", "created_at"],
    )

    op.create_table(
        "retrieval_traces",
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("index_version", sa.String(length=256), nullable=True),
        sa.Column("dense_model_version", sa.String(length=256), nullable=False),
        sa.Column("sparse_model_version", sa.String(length=256), nullable=True),
        sa.Column("rerank_model_version", sa.String(length=256), nullable=True),
        sa.Column("prompt_version", sa.String(length=256), nullable=False),
        sa.Column("retrieval_policy_version", sa.String(length=256), nullable=False),
        sa.Column("threshold_version", sa.String(length=256), nullable=False),
        sa.Column("stage_metrics_json", sa.JSON(), nullable=False),
        sa.Column("dense_candidate_count", sa.Integer(), nullable=False),
        sa.Column("sparse_candidate_count", sa.Integer(), nullable=False),
        sa.Column("rerank_candidate_count", sa.Integer(), nullable=False),
        sa.Column("positive_result_count", sa.Integer(), nullable=False),
        sa.Column("negative_result_count", sa.Integer(), nullable=False),
        sa.Column("positive_example_ids_json", sa.JSON(), nullable=False),
        sa.Column("negative_example_ids_json", sa.JSON(), nullable=False),
        sa.Column("empty_retrieval", sa.Boolean(), nullable=False),
        sa.Column("positive_hit_rate", sa.Float(), nullable=False),
        sa.Column("negative_hit_rate", sa.Float(), nullable=False),
        sa.Column("review_required", sa.Boolean(), nullable=True),
        sa.Column("remote_model_error_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost", sa.Float(), nullable=True),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "dense_candidate_count >= 0 AND sparse_candidate_count >= 0 "
            "AND rerank_candidate_count >= 0 AND positive_result_count >= 0 "
            "AND negative_result_count >= 0 AND remote_model_error_count >= 0",
            name="ck_retrieval_traces_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "positive_hit_rate >= 0 AND positive_hit_rate <= 1 "
            "AND negative_hit_rate >= 0 AND negative_hit_rate <= 1",
            name="ck_retrieval_traces_hit_rates",
        ),
        sa.CheckConstraint(
            "(succeeded AND error_code IS NULL) OR "
            "(NOT succeeded AND error_code IS NOT NULL)",
            name="ck_retrieval_traces_outcome",
        ),
        sa.PrimaryKeyConstraint("trace_id", name="pk_retrieval_traces"),
    )
    op.create_index(
        "ix_retrieval_traces_tenant_id",
        "retrieval_traces",
        ["tenant_id"],
    )
    op.create_index(
        "ix_retrieval_traces_tenant_index_created",
        "retrieval_traces",
        ["tenant_id", "index_version", "created_at"],
    )
    op.create_index(
        "ix_retrieval_traces_tenant_scope",
        "retrieval_traces",
        ["tenant_id", "document_type", "field_path", "schema_version"],
    )

    op.create_table(
        "memory_retrieval_feedback",
        sa.Column("feedback_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "label IN ('helpful', 'not_relevant', 'misleading')",
            name="ck_memory_retrieval_feedback_label",
        ),
        sa.CheckConstraint(
            "label = 'helpful' OR reason IS NOT NULL",
            name="ck_memory_retrieval_feedback_reason",
        ),
        sa.ForeignKeyConstraint(
            ["trace_id"],
            ["retrieval_traces.trace_id"],
            name="fk_memory_retrieval_feedback_trace_id_retrieval_traces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["example_id"],
            ["reviewed_examples.example_id"],
            name="fk_memory_retrieval_feedback_example_id_reviewed_examples",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("feedback_id", name="pk_memory_retrieval_feedback"),
        sa.UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_memory_retrieval_feedback_fingerprint",
        ),
    )
    op.create_index(
        "ix_memory_retrieval_feedback_tenant_id",
        "memory_retrieval_feedback",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_retrieval_feedback_trace",
        "memory_retrieval_feedback",
        ["tenant_id", "trace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_retrieval_feedback_trace",
        table_name="memory_retrieval_feedback",
    )
    op.drop_index(
        "ix_memory_retrieval_feedback_tenant_id",
        table_name="memory_retrieval_feedback",
    )
    op.drop_table("memory_retrieval_feedback")
    op.drop_index("ix_retrieval_traces_tenant_scope", table_name="retrieval_traces")
    op.drop_index(
        "ix_retrieval_traces_tenant_index_created",
        table_name="retrieval_traces",
    )
    op.drop_index("ix_retrieval_traces_tenant_id", table_name="retrieval_traces")
    op.drop_table("retrieval_traces")
    op.drop_index(
        "ix_memory_governance_audits_resource",
        table_name="memory_governance_audits",
    )
    op.drop_index(
        "ix_memory_governance_audits_tenant_id",
        table_name="memory_governance_audits",
    )
    op.drop_table("memory_governance_audits")
