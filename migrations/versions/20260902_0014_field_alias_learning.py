"""Add controlled field-alias learning facts and conflict links.

Revision ID: 20260902_0014
Revises: 20260902_0013
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0014"
down_revision: str | None = "20260902_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("field_semantic_catalog_versions") as batch:
        batch.drop_constraint(
            "ck_field_semantic_catalog_versions_validity",
            type_="check",
        )
        batch.add_column(sa.Column("invalidated_reason", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_field_semantic_catalog_versions_validity",
            "(is_valid AND invalidated_at IS NULL AND invalidated_reason IS NULL) OR "
            "(NOT is_valid AND NOT is_active AND invalidated_at IS NOT NULL "
            "AND invalidated_reason IS NOT NULL)",
        )

    op.create_table(
        "field_alias_candidates",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=True),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("canonical_field_path", sa.Text(), nullable=False),
        sa.Column("alias_text", sa.Text(), nullable=False),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("context_anchors_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("canonical_collision", sa.Boolean(), nullable=False),
        sa.Column("support_window_days", sa.Integer(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("promoted_catalog_version", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "(scope = 'tenant' AND tenant_id IS NOT NULL) OR "
            "(scope = 'global' AND tenant_id IS NULL)",
            name="ck_field_alias_candidates_scope",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_alias_candidates_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(status != 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND review_reason IS NOT NULL)",
            name="ck_field_alias_candidates_review_metadata",
        ),
        sa.CheckConstraint(
            "(scope = 'global' AND promoted_catalog_version IS NULL) OR "
            "(scope = 'tenant' AND status IN ('approved', 'suspended') "
            "AND promoted_catalog_version IS NOT NULL) OR "
            "(scope = 'tenant' AND status IN ('pending', 'rejected') "
            "AND promoted_catalog_version IS NULL) OR "
            "(scope = 'tenant' AND status = 'invalidated')",
            name="ck_field_alias_candidates_promotion",
        ),
        sa.CheckConstraint(
            "scope != 'global' OR promoted_catalog_version IS NULL",
            name="ck_field_alias_candidates_global_no_catalog",
        ),
        sa.CheckConstraint(
            "support_window_days > 0",
            name="ck_field_alias_candidates_support_window",
        ),
        sa.CheckConstraint(
            "updated_at >= submitted_at AND "
            "(reviewed_at IS NULL OR reviewed_at >= submitted_at)",
            name="ck_field_alias_candidates_timestamps",
        ),
        sa.PrimaryKeyConstraint("candidate_id", name="pk_field_alias_candidates"),
    )
    op.create_index(
        "ix_field_alias_candidates_tenant_id",
        "field_alias_candidates",
        ["tenant_id"],
    )
    op.create_index(
        "ix_field_alias_candidates_competing",
        "field_alias_candidates",
        [
            "tenant_id",
            "schema_version",
            "document_type",
            "normalized_alias",
            "status",
        ],
    )

    op.create_table(
        "field_alias_candidate_sources",
        sa.Column("support_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_id", sa.String(length=64), nullable=False),
        sa.Column("binding_decision_id", sa.String(length=64), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("source_catalog_version", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("template_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["field_alias_candidates.candidate_id"],
            name="fk_field_alias_sources_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "support_id",
            name="pk_field_alias_candidate_sources",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "evidence_id",
            "binding_decision_id",
            name="uq_field_alias_candidate_sources_review",
        ),
    )
    op.create_index(
        "ix_field_alias_candidate_sources_candidate_id",
        "field_alias_candidate_sources",
        ["candidate_id"],
    )
    op.create_index(
        "ix_field_alias_candidate_sources_tenant_id",
        "field_alias_candidate_sources",
        ["tenant_id"],
    )
    op.create_index(
        "ix_field_alias_candidate_sources_window",
        "field_alias_candidate_sources",
        ["tenant_id", "candidate_id", "occurred_at"],
    )

    op.create_table(
        "field_alias_candidate_decisions",
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("promoted_catalog_version", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "scope IN ('tenant', 'global')",
            name="ck_field_alias_candidate_decisions_scope",
        ),
        sa.CheckConstraint(
            "previous_status IN ('pending', 'approved', 'rejected', 'suspended')",
            name="ck_field_alias_candidate_decisions_previous_status",
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_alias_candidate_decisions_status",
        ),
        sa.CheckConstraint(
            "previous_status != status",
            name="ck_field_alias_candidate_decisions_transition",
        ),
        sa.CheckConstraint(
            "(scope = 'tenant' AND authority = 'tenant_governor') OR "
            "(scope = 'global' AND authority = 'global_governor')",
            name="ck_field_alias_candidate_decisions_authority",
        ),
        sa.CheckConstraint(
            "(scope = 'tenant' AND status = 'approved' "
            "AND promoted_catalog_version IS NOT NULL) OR "
            "(scope = 'global' AND status = 'approved' "
            "AND promoted_catalog_version IS NULL) OR "
            "(status != 'approved' AND promoted_catalog_version IS NULL)",
            name="ck_field_alias_candidate_decisions_promotion",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["field_alias_candidates.candidate_id"],
            name="fk_field_alias_decisions_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "decision_id",
            name="pk_field_alias_candidate_decisions",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "idempotency_key_hash",
            name="uq_field_alias_candidate_decisions_idempotency",
        ),
    )
    op.create_index(
        "ix_field_alias_candidate_decisions_candidate_id",
        "field_alias_candidate_decisions",
        ["candidate_id"],
    )

    op.create_table(
        "field_alias_global_support_snapshots",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("salt_version", sa.String(length=128), nullable=False),
        sa.Column("tenant_fingerprints_json", sa.JSON(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("distinct_documents", sa.Integer(), nullable=False),
        sa.Column("distinct_templates", sa.Integer(), nullable=False),
        sa.Column("distinct_reviewers", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_count >= 0 AND distinct_documents >= 0 "
            "AND distinct_templates >= 0 AND distinct_reviewers >= 0 "
            "AND distinct_documents <= source_count "
            "AND distinct_templates <= source_count "
            "AND distinct_reviewers <= source_count",
            name="ck_field_alias_global_support_counts",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["field_alias_candidates.candidate_id"],
            name="fk_field_alias_global_support_candidate",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "candidate_id",
            name="pk_field_alias_global_support_snapshots",
        ),
    )

    with op.batch_alter_table("memory_conflicts") as batch:
        batch.add_column(
            sa.Column(
                "candidate_field_paths_json",
                sa.JSON(),
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )

    op.create_table(
        "memory_conflict_field_alias_candidates",
        sa.Column("conflict_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_memory_conflict_field_alias_candidates_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["field_alias_candidates.candidate_id"],
            name="fk_memory_conflict_alias_candidates_candidate",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conflict_id"],
            ["memory_conflicts.conflict_id"],
            name="fk_memory_conflict_alias_candidates_conflict",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conflict_id",
            "candidate_id",
            name="pk_memory_conflict_field_alias_candidates",
        ),
        sa.UniqueConstraint(
            "conflict_id",
            "ordinal",
            name="uq_memory_conflict_field_alias_candidates_ordinal",
        ),
    )
    op.create_index(
        "ix_memory_conflict_field_alias_candidates_lookup",
        "memory_conflict_field_alias_candidates",
        ["tenant_id", "candidate_id", "conflict_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_conflict_field_alias_candidates_lookup",
        table_name="memory_conflict_field_alias_candidates",
    )
    op.drop_table("memory_conflict_field_alias_candidates")
    with op.batch_alter_table("memory_conflicts") as batch:
        batch.drop_column("candidate_field_paths_json")

    op.drop_table("field_alias_global_support_snapshots")
    op.drop_index(
        "ix_field_alias_candidate_decisions_candidate_id",
        table_name="field_alias_candidate_decisions",
    )
    op.drop_table("field_alias_candidate_decisions")
    op.drop_index(
        "ix_field_alias_candidate_sources_window",
        table_name="field_alias_candidate_sources",
    )
    op.drop_index(
        "ix_field_alias_candidate_sources_tenant_id",
        table_name="field_alias_candidate_sources",
    )
    op.drop_index(
        "ix_field_alias_candidate_sources_candidate_id",
        table_name="field_alias_candidate_sources",
    )
    op.drop_table("field_alias_candidate_sources")
    op.drop_index(
        "ix_field_alias_candidates_competing",
        table_name="field_alias_candidates",
    )
    op.drop_index(
        "ix_field_alias_candidates_tenant_id",
        table_name="field_alias_candidates",
    )
    op.drop_table("field_alias_candidates")

    with op.batch_alter_table("field_semantic_catalog_versions") as batch:
        batch.drop_constraint(
            "ck_field_semantic_catalog_versions_validity",
            type_="check",
        )
        batch.drop_column("invalidated_reason")
        batch.create_check_constraint(
            "ck_field_semantic_catalog_versions_validity",
            "is_valid OR (NOT is_active AND invalidated_at IS NOT NULL)",
        )
