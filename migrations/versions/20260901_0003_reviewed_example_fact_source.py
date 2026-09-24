"""Add multi-tenant reviewed-example RAG fact source.

Revision ID: 20260901_0003
Revises: 20260901_0002
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0003"
down_revision: str | None = "20260901_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("model_version_id", name="pk_model_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_model_versions_tenant_version",
        ),
    )
    op.create_index("ix_model_versions_tenant_id", "model_versions", ["tenant_id"])

    op.create_table(
        "prompt_versions",
        sa.Column("prompt_version_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("prompt_version_id", name="pk_prompt_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_prompt_versions_tenant_version",
        ),
    )
    op.create_index("ix_prompt_versions_tenant_id", "prompt_versions", ["tenant_id"])

    op.create_table(
        "index_versions",
        sa.Column("index_version_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=256), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("dense_model_version_id", sa.String(length=64), nullable=False),
        sa.Column("sparse_model_version_id", sa.String(length=64), nullable=True),
        sa.Column("rerank_model_version_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("invalidated_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["dense_model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_index_versions_dense_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sparse_model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_index_versions_sparse_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rerank_model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_index_versions_rerank_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.prompt_version_id"],
            name="fk_index_versions_prompt_version_id_prompt_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("index_version_id", name="pk_index_versions"),
        sa.UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_index_versions_tenant_version",
        ),
    )
    op.create_index("ix_index_versions_tenant_id", "index_versions", ["tenant_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.create_index(
            "uq_index_versions_tenant_active",
            "index_versions",
            ["tenant_id"],
            unique=True,
            postgresql_where=sa.text("is_active"),
        )
    elif dialect == "sqlite":
        op.create_index(
            "uq_index_versions_tenant_active",
            "index_versions",
            ["tenant_id"],
            unique=True,
            sqlite_where=sa.text("is_active = 1"),
        )

    op.create_table(
        "example_feedback",
        sa.Column("feedback_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("replay_key", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("label_type", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("source_event_id", sa.String(length=64), nullable=True),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version_id", sa.String(length=64), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "label_type IN ('confirmed_correct', 'corrected', 'confirmed_incorrect')",
            name="ck_example_feedback_label_type",
        ),
        sa.CheckConstraint(
            "(label_type = 'corrected' AND source_event_id IS NOT NULL) OR "
            "(label_type IN ('confirmed_correct', 'confirmed_incorrect') "
            "AND source_event_id IS NULL)",
            name="ck_example_feedback_review_source",
        ),
        sa.CheckConstraint(
            "label_type != 'corrected' OR reason IS NOT NULL",
            name="ck_example_feedback_corrected_reason",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_example_feedback_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_example_feedback_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.prompt_version_id"],
            name="fk_example_feedback_prompt_version_id_prompt_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.run_id"],
            name="fk_example_feedback_run_id_extraction_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["correction_events.event_id"],
            name="fk_example_feedback_source_event_id_correction_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("feedback_id", name="pk_example_feedback"),
        sa.UniqueConstraint(
            "tenant_id",
            "replay_key",
            name="uq_example_feedback_tenant_replay",
        ),
    )
    op.create_index("ix_example_feedback_tenant_id", "example_feedback", ["tenant_id"])

    op.create_table(
        "reviewed_examples",
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("replay_key", sa.String(length=64), nullable=False),
        sa.Column("source_feedback_id", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.String(length=64), nullable=True),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version_id", sa.String(length=64), nullable=False),
        sa.Column("label_type", sa.String(length=32), nullable=False),
        sa.Column("model_value_json", sa.JSON(), nullable=True),
        sa.Column("reviewed_value_json", sa.JSON(), nullable=True),
        sa.Column("correction_reason", sa.Text(), nullable=True),
        sa.Column("vendor_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("template_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("evidence_reference_json", sa.JSON(), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("is_reviewed", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("invalidated_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "label_type IN ('confirmed_correct', 'corrected', 'confirmed_incorrect')",
            name="ck_reviewed_examples_label_type",
        ),
        sa.CheckConstraint(
            "(label_type = 'corrected' AND source_event_id IS NOT NULL) OR "
            "(label_type IN ('confirmed_correct', 'confirmed_incorrect') "
            "AND source_event_id IS NULL)",
            name="ck_reviewed_examples_review_source",
        ),
        sa.CheckConstraint(
            "label_type != 'corrected' OR correction_reason IS NOT NULL",
            name="ck_reviewed_examples_corrected_reason",
        ),
        sa.CheckConstraint("is_reviewed", name="ck_reviewed_examples_is_reviewed"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_reviewed_examples_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_reviewed_examples_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.prompt_version_id"],
            name="fk_reviewed_examples_prompt_version_id_prompt_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.run_id"],
            name="fk_reviewed_examples_run_id_extraction_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["correction_events.event_id"],
            name="fk_reviewed_examples_source_event_id_correction_events",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_feedback_id"],
            ["example_feedback.feedback_id"],
            name="fk_reviewed_examples_source_feedback_id_example_feedback",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("example_id", name="pk_reviewed_examples"),
        sa.UniqueConstraint(
            "source_feedback_id",
            name="uq_reviewed_examples_source_feedback_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "replay_key",
            name="uq_reviewed_examples_tenant_replay",
        ),
        sa.UniqueConstraint(
            "source_event_id",
            name="uq_reviewed_examples_source_event_id",
        ),
    )
    op.create_index("ix_reviewed_examples_tenant_id", "reviewed_examples", ["tenant_id"])
    op.create_index(
        "ix_reviewed_examples_scope_active",
        "reviewed_examples",
        [
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
            "is_reviewed",
            "is_valid",
        ],
    )

    op.create_table(
        "example_index_projections",
        sa.Column("projection_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("index_version_id", sa.String(length=64), nullable=False),
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
            name="ck_example_index_projections_status",
        ),
        sa.ForeignKeyConstraint(
            ["example_id"],
            ["reviewed_examples.example_id"],
            name="fk_example_index_projections_example_id_reviewed_examples",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["index_version_id"],
            ["index_versions.index_version_id"],
            name="fk_example_index_projections_index_version_id_index_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("projection_id", name="pk_example_index_projections"),
        sa.UniqueConstraint(
            "tenant_id",
            "example_id",
            "index_version_id",
            name="uq_example_index_projections_scope",
        ),
    )
    op.create_index(
        "ix_example_index_projections_tenant_id",
        "example_index_projections",
        ["tenant_id"],
    )
    op.create_index(
        "ix_example_index_projections_queue",
        "example_index_projections",
        ["tenant_id", "index_version_id", "status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_example_index_projections_queue",
        table_name="example_index_projections",
    )
    op.drop_index(
        "ix_example_index_projections_tenant_id",
        table_name="example_index_projections",
    )
    op.drop_table("example_index_projections")
    op.drop_index("ix_reviewed_examples_scope_active", table_name="reviewed_examples")
    op.drop_index("ix_reviewed_examples_tenant_id", table_name="reviewed_examples")
    op.drop_table("reviewed_examples")
    op.drop_index("ix_example_feedback_tenant_id", table_name="example_feedback")
    op.drop_table("example_feedback")
    if op.get_bind().dialect.name in {"postgresql", "sqlite"}:
        op.drop_index("uq_index_versions_tenant_active", table_name="index_versions")
    op.drop_index("ix_index_versions_tenant_id", table_name="index_versions")
    op.drop_table("index_versions")
    op.drop_index("ix_prompt_versions_tenant_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")
    op.drop_index("ix_model_versions_tenant_id", table_name="model_versions")
    op.drop_table("model_versions")
