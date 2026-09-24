"""Add rebuildable field semantic index projection state.

Revision ID: 20260902_0012
Revises: 20260902_0011
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0012"
down_revision: str | None = "20260902_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "field_semantic_index_versions",
        sa.Column("index_version_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=256), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.String(length=128), nullable=False),
        sa.Column("dense_model_version_id", sa.String(length=64), nullable=False),
        sa.Column("sparse_model_version_id", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("invalidated_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(is_valid AND invalidated_at IS NULL AND invalidated_reason IS NULL) OR "
            "(NOT is_valid AND NOT is_active AND invalidated_at IS NOT NULL "
            "AND invalidated_reason IS NOT NULL)",
            name="ck_field_semantic_index_versions_validity",
        ),
        sa.ForeignKeyConstraint(
            ["dense_model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_field_semantic_index_versions_dense_model",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sparse_model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_field_semantic_index_versions_sparse_model",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "index_version_id",
            name="pk_field_semantic_index_versions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_field_semantic_index_versions_tenant_version",
        ),
    )
    op.create_index(
        "ix_field_semantic_index_versions_tenant_id",
        "field_semantic_index_versions",
        ["tenant_id"],
    )
    op.create_index(
        "uq_field_semantic_index_versions_active",
        "field_semantic_index_versions",
        ["tenant_id", "schema_version"],
        unique=True,
        postgresql_where=sa.text("is_active"),
        sqlite_where=sa.text("is_active = 1"),
    )
    op.create_index(
        "ix_field_semantic_index_versions_catalog",
        "field_semantic_index_versions",
        ["tenant_id", "schema_version", "catalog_version", "is_valid"],
    )

    op.create_table(
        "field_semantic_index_projections",
        sa.Column("projection_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("semantic_id", sa.String(length=64), nullable=False),
        sa.Column("index_version_id", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("canonical_field_path", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("catalog_version", sa.String(length=128), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("approved_aliases_json", sa.JSON(), nullable=False),
        sa.Column("negative_aliases_json", sa.JSON(), nullable=False),
        sa.Column("context_anchors_json", sa.JSON(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("projection_checksum", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'indexed', 'failed', 'invalidated')",
            name="ck_field_semantic_index_projections_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_field_semantic_index_projections_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["field_semantic_index_versions.index_version_id"],
            name="fk_field_semantic_index_projections_version",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "projection_id",
            name="pk_field_semantic_index_projections",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "semantic_id",
            "index_version_id",
            name="uq_field_semantic_index_projections_scope",
        ),
    )
    op.create_index(
        "ix_field_semantic_index_projections_tenant_id",
        "field_semantic_index_projections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_field_semantic_index_projections_queue",
        "field_semantic_index_projections",
        ["tenant_id", "index_version_id", "status", "updated_at"],
    )
    op.create_index(
        "ix_field_semantic_index_projections_scope",
        "field_semantic_index_projections",
        ["tenant_id", "document_type", "schema_version", "catalog_version", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_field_semantic_index_projections_scope",
        table_name="field_semantic_index_projections",
    )
    op.drop_index(
        "ix_field_semantic_index_projections_queue",
        table_name="field_semantic_index_projections",
    )
    op.drop_index(
        "ix_field_semantic_index_projections_tenant_id",
        table_name="field_semantic_index_projections",
    )
    op.drop_table("field_semantic_index_projections")
    op.drop_index(
        "ix_field_semantic_index_versions_catalog",
        table_name="field_semantic_index_versions",
    )
    op.drop_index(
        "uq_field_semantic_index_versions_active",
        table_name="field_semantic_index_versions",
    )
    op.drop_index(
        "ix_field_semantic_index_versions_tenant_id",
        table_name="field_semantic_index_versions",
    )
    op.drop_table("field_semantic_index_versions")
