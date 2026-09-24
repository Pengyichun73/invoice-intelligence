"""Add controlled hard-negative mining and training dataset facts.

Revision ID: 20260901_0006
Revises: 20260901_0005
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0006"
down_revision: str | None = "20260901_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hard_negative_retrieval_judgments",
        sa.Column("judgment_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("query_example_id", sa.String(length=64), nullable=False),
        sa.Column("retrieved_example_id", sa.String(length=64), nullable=False),
        sa.Column("scores_json", sa.JSON(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("is_relevant", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("judgment_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rank > 0", name="ck_hard_negative_judgments_rank"),
        sa.CheckConstraint(
            "NOT is_relevant",
            name="ck_hard_negative_judgments_rejected",
        ),
        sa.CheckConstraint(
            "query_example_id != retrieved_example_id",
            name="ck_hard_negative_judgments_distinct_examples",
        ),
        sa.ForeignKeyConstraint(
            ["query_example_id"],
            ["reviewed_examples.example_id"],
            name="fk_hard_negative_judgments_query_example",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["retrieved_example_id"],
            ["reviewed_examples.example_id"],
            name="fk_hard_negative_judgments_retrieved_example",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "judgment_id",
            name="pk_hard_negative_retrieval_judgments",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_hard_negative_judgments_tenant_fingerprint",
        ),
    )
    op.create_index(
        "ix_hard_negative_retrieval_judgments_tenant_id",
        "hard_negative_retrieval_judgments",
        ["tenant_id"],
    )
    op.create_index(
        "ix_hard_negative_judgments_scope",
        "hard_negative_retrieval_judgments",
        ["tenant_id", "schema_version", "is_valid", "judgment_id"],
    )

    op.create_table(
        "hard_negative_candidates",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("positive_example_id", sa.String(length=64), nullable=False),
        sa.Column("negative_example_id", sa.String(length=64), nullable=False),
        sa.Column("signal_types_json", sa.JSON(), nullable=False),
        sa.Column("proposal_source", sa.String(length=48), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_judgment_id", sa.String(length=64), nullable=True),
        sa.Column("review_id", sa.String(length=64), nullable=True),
        sa.Column("reviewer_id", sa.String(length=128), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("candidate_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_hard_negative_candidates_status",
        ),
        sa.CheckConstraint(
            "positive_example_id != negative_example_id",
            name="ck_hard_negative_candidates_distinct_examples",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND review_id IS NULL AND reviewer_id IS NULL "
            "AND review_reason IS NULL AND reviewed_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND review_id IS NOT NULL "
            "AND reviewer_id IS NOT NULL AND review_reason IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_hard_negative_candidates_human_gate",
        ),
        sa.ForeignKeyConstraint(
            ["positive_example_id"],
            ["reviewed_examples.example_id"],
            name="fk_hard_negative_candidates_positive_example",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["negative_example_id"],
            ["reviewed_examples.example_id"],
            name="fk_hard_negative_candidates_negative_example",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_judgment_id"],
            ["hard_negative_retrieval_judgments.judgment_id"],
            name="fk_hard_negative_candidates_source_judgment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_hard_negative_candidates"),
        sa.UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_hard_negative_candidates_tenant_fingerprint",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "review_id",
            name="uq_hard_negative_candidates_tenant_review",
        ),
    )
    op.create_index(
        "ix_hard_negative_candidates_tenant_id",
        "hard_negative_candidates",
        ["tenant_id"],
    )
    op.create_index(
        "ix_hard_negative_candidates_export",
        "hard_negative_candidates",
        ["tenant_id", "schema_version", "status", "is_valid", "candidate_id"],
    )

    op.create_table(
        "training_dataset_versions",
        sa.Column("dataset_key", sa.String(length=64), nullable=False),
        sa.Column("tenant_scope", sa.String(length=128), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("source_tenant_ids_json", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("generation_rule_version", sa.String(length=128), nullable=False),
        sa.Column("redaction_policy_version", sa.String(length=128), nullable=False),
        sa.Column("split_rule_version", sa.String(length=128), nullable=False),
        sa.Column("split_salt_version", sa.String(length=128), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("cross_tenant", sa.Boolean(), nullable=False),
        sa.Column("authorization_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dataset_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('building', 'exported', 'failed')",
            name="ck_training_dataset_versions_status",
        ),
        sa.CheckConstraint(
            "record_count > 0",
            name="ck_training_dataset_versions_records",
        ),
        sa.CheckConstraint(
            "(cross_tenant AND authorization_id IS NOT NULL) OR "
            "(NOT cross_tenant AND authorization_id IS NULL)",
            name="ck_training_dataset_versions_authorization",
        ),
        sa.PrimaryKeyConstraint("dataset_key", name="pk_training_dataset_versions"),
        sa.UniqueConstraint(
            "tenant_scope",
            "dataset_id",
            "dataset_version",
            name="uq_training_dataset_versions_scope_identity",
        ),
        sa.UniqueConstraint(
            "tenant_scope",
            "fingerprint",
            name="uq_training_dataset_versions_scope_fingerprint",
        ),
    )
    op.create_index(
        "ix_training_dataset_versions_tenant_scope",
        "training_dataset_versions",
        ["tenant_scope"],
    )
    op.create_index(
        "ix_training_dataset_versions_scope_schema",
        "training_dataset_versions",
        ["tenant_scope", "schema_version", "status"],
    )

    op.create_table(
        "training_dataset_records",
        sa.Column("record_id", sa.String(length=64), nullable=False),
        sa.Column("dataset_key", sa.String(length=64), nullable=False),
        sa.Column("source_tenant_id", sa.String(length=128), nullable=False),
        sa.Column("split", sa.String(length=32), nullable=False),
        sa.Column("source_document_ids_json", sa.JSON(), nullable=False),
        sa.Column("candidate_ids_json", sa.JSON(), nullable=False),
        sa.Column("group_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("record_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "split IN ('train', 'validation', 'evaluation')",
            name="ck_training_dataset_records_split",
        ),
        sa.ForeignKeyConstraint(
            ["dataset_key"],
            ["training_dataset_versions.dataset_key"],
            name="fk_training_dataset_records_dataset_key",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("record_id", name="pk_training_dataset_records"),
        sa.UniqueConstraint(
            "dataset_key",
            "fingerprint",
            name="uq_training_dataset_records_dataset_fingerprint",
        ),
    )
    op.create_index(
        "ix_training_dataset_records_source_tenant_id",
        "training_dataset_records",
        ["source_tenant_id"],
    )
    op.create_index(
        "ix_training_dataset_records_dataset_split",
        "training_dataset_records",
        ["dataset_key", "split", "record_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_training_dataset_records_dataset_split",
        table_name="training_dataset_records",
    )
    op.drop_index(
        "ix_training_dataset_records_source_tenant_id",
        table_name="training_dataset_records",
    )
    op.drop_table("training_dataset_records")
    op.drop_index(
        "ix_training_dataset_versions_scope_schema",
        table_name="training_dataset_versions",
    )
    op.drop_index(
        "ix_training_dataset_versions_tenant_scope",
        table_name="training_dataset_versions",
    )
    op.drop_table("training_dataset_versions")
    op.drop_index(
        "ix_hard_negative_candidates_export",
        table_name="hard_negative_candidates",
    )
    op.drop_index(
        "ix_hard_negative_candidates_tenant_id",
        table_name="hard_negative_candidates",
    )
    op.drop_table("hard_negative_candidates")
    op.drop_index(
        "ix_hard_negative_judgments_scope",
        table_name="hard_negative_retrieval_judgments",
    )
    op.drop_index(
        "ix_hard_negative_retrieval_judgments_tenant_id",
        table_name="hard_negative_retrieval_judgments",
    )
    op.drop_table("hard_negative_retrieval_judgments")
