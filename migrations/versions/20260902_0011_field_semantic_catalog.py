"""Add tenant-scoped field semantic catalog facts.

Revision ID: 20260902_0011
Revises: 20260902_0010
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0011"
down_revision: str | None = "20260902_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "field_semantic_catalog_versions",
        sa.Column("catalog_version_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.String(length=128), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(length=128), nullable=True),
        sa.Column("activation_reason", sa.Text(), nullable=True),
        sa.Column("activation_idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(is_active AND is_valid AND activated_by IS NOT NULL "
            "AND activation_reason IS NOT NULL "
            "AND activation_idempotency_key_hash IS NOT NULL "
            "AND activated_at IS NOT NULL AND retired_at IS NULL "
            "AND invalidated_at IS NULL) OR NOT is_active",
            name="ck_field_semantic_catalog_versions_active_metadata",
        ),
        sa.CheckConstraint(
            "is_valid OR (NOT is_active AND invalidated_at IS NOT NULL)",
            name="ck_field_semantic_catalog_versions_validity",
        ),
        sa.PrimaryKeyConstraint(
            "catalog_version_id",
            name="pk_field_semantic_catalog_versions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "schema_version",
            "catalog_version",
            name="uq_field_semantic_catalog_versions_scope",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "activation_idempotency_key_hash",
            name="uq_field_semantic_catalog_versions_activation_idempotency",
        ),
    )
    op.create_index(
        "ix_field_semantic_catalog_versions_tenant_id",
        "field_semantic_catalog_versions",
        ["tenant_id"],
    )
    op.create_index(
        "uq_field_semantic_catalog_versions_active",
        "field_semantic_catalog_versions",
        ["tenant_id", "schema_version"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )

    op.create_table(
        "field_semantic_aliases",
        sa.Column("alias_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("catalog_version_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("canonical_field_path", sa.Text(), nullable=False),
        sa.Column("alias_text", sa.Text(), nullable=False),
        sa.Column("normalized_alias", sa.Text(), nullable=False),
        sa.Column("is_negative", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("submitted_by", sa.String(length=128), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_semantic_aliases_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(status != 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND review_reason IS NOT NULL)",
            name="ck_field_semantic_aliases_review_metadata",
        ),
        sa.CheckConstraint(
            "(status = 'invalidated' AND NOT is_valid) OR "
            "(status != 'invalidated' AND is_valid)",
            name="ck_field_semantic_aliases_validity",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id"],
            ["field_semantic_catalog_versions.catalog_version_id"],
            name="fk_field_aliases_catalog_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("alias_id", name="pk_field_semantic_aliases"),
        sa.UniqueConstraint(
            "tenant_id",
            "catalog_version_id",
            "document_type",
            "canonical_field_path",
            "normalized_alias",
            "is_negative",
            name="uq_field_semantic_aliases_semantics",
        ),
    )
    op.create_index(
        "ix_field_semantic_aliases_tenant_id",
        "field_semantic_aliases",
        ["tenant_id"],
    )
    op.create_index(
        "ix_field_semantic_aliases_binding",
        "field_semantic_aliases",
        [
            "tenant_id",
            "schema_version",
            "catalog_version_id",
            "document_type",
            "canonical_field_path",
            "status",
            "is_valid",
        ],
    )

    op.create_table(
        "field_semantic_alias_decisions",
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("alias_id", sa.String(length=64), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "previous_status IN ('pending', 'approved', 'rejected', 'suspended')",
            name="ck_field_semantic_alias_decisions_previous_status",
        ),
        sa.CheckConstraint(
            "status IN ('approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_semantic_alias_decisions_status",
        ),
        sa.CheckConstraint(
            "previous_status != status",
            name="ck_field_semantic_alias_decisions_transition",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_field_semantic_alias_decisions_revision",
        ),
        sa.ForeignKeyConstraint(
            ["alias_id"],
            ["field_semantic_aliases.alias_id"],
            name="fk_field_alias_decisions_alias",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "decision_id",
            name="pk_field_semantic_alias_decisions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "alias_id",
            "revision",
            name="uq_field_semantic_alias_decisions_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_field_semantic_alias_decisions_idempotency",
        ),
    )
    op.create_index(
        "ix_field_semantic_alias_decisions_tenant_id",
        "field_semantic_alias_decisions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_field_semantic_alias_decisions_alias_revision",
        "field_semantic_alias_decisions",
        ["tenant_id", "alias_id", "revision"],
    )

    op.create_table(
        "field_semantic_context_anchors",
        sa.Column("anchor_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("alias_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("relation", sa.String(length=32), nullable=False),
        sa.Column("max_distance", sa.Integer(), nullable=True),
        sa.Column("is_negative", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "relation IN ('same_line', 'preceding', 'following', 'same_block', "
            "'document_section')",
            name="ck_field_semantic_context_anchors_relation",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_field_semantic_context_anchors_ordinal",
        ),
        sa.CheckConstraint(
            "max_distance IS NULL OR max_distance > 0",
            name="ck_field_semantic_context_anchors_distance",
        ),
        sa.ForeignKeyConstraint(
            ["alias_id"],
            ["field_semantic_aliases.alias_id"],
            name="fk_field_context_anchors_alias",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "alias_id",
            "anchor_id",
            name="pk_field_semantic_context_anchors",
        ),
        sa.UniqueConstraint(
            "alias_id",
            "ordinal",
            name="uq_field_semantic_context_anchors_ordinal",
        ),
    )
    op.create_index(
        "ix_field_semantic_context_anchors_tenant_id",
        "field_semantic_context_anchors",
        ["tenant_id"],
    )
    op.create_index(
        "ix_field_semantic_context_anchors_alias",
        "field_semantic_context_anchors",
        ["tenant_id", "alias_id", "ordinal"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_field_semantic_context_anchors_alias",
        table_name="field_semantic_context_anchors",
    )
    op.drop_index(
        "ix_field_semantic_context_anchors_tenant_id",
        table_name="field_semantic_context_anchors",
    )
    op.drop_table("field_semantic_context_anchors")
    op.drop_index(
        "ix_field_semantic_alias_decisions_alias_revision",
        table_name="field_semantic_alias_decisions",
    )
    op.drop_index(
        "ix_field_semantic_alias_decisions_tenant_id",
        table_name="field_semantic_alias_decisions",
    )
    op.drop_table("field_semantic_alias_decisions")
    op.drop_index(
        "ix_field_semantic_aliases_binding",
        table_name="field_semantic_aliases",
    )
    op.drop_index(
        "ix_field_semantic_aliases_tenant_id",
        table_name="field_semantic_aliases",
    )
    op.drop_table("field_semantic_aliases")
    op.drop_index(
        "uq_field_semantic_catalog_versions_active",
        table_name="field_semantic_catalog_versions",
    )
    op.drop_index(
        "ix_field_semantic_catalog_versions_tenant_id",
        table_name="field_semantic_catalog_versions",
    )
    op.drop_table("field_semantic_catalog_versions")
