"""Add reviewed correction events and PostgreSQL pgvector memory.

Revision ID: 20260901_0002
Revises: 20260901_0001
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "20260901_0002"
down_revision: str | None = "20260901_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_correction_events_checksum_created",
        table_name="correction_events",
    )
    with op.batch_alter_table("correction_events") as batch:
        batch.add_column(
            sa.Column(
                "document_type",
                sa.String(length=128),
                nullable=False,
                server_default="legacy",
            )
        )
        batch.alter_column(
            "incorrect_json",
            new_column_name="model_value_json",
            existing_type=sa.JSON(),
            existing_nullable=True,
        )
        batch.alter_column(
            "correct_json",
            new_column_name="corrected_value_json",
            existing_type=sa.JSON(),
            existing_nullable=True,
        )
        batch.alter_column(
            "reason",
            new_column_name="correction_reason",
            existing_type=sa.Text(),
            existing_nullable=False,
        )
        batch.add_column(
            sa.Column(
                "vendor_features_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column(
                "template_features_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column(
                "document_reference",
                sa.Text(),
                nullable=False,
                server_default="document:legacy",
            )
        )
        batch.alter_column(
            "context_reference",
            new_column_name="image_reference",
            existing_type=sa.Text(),
            existing_nullable=True,
        )
        batch.add_column(
            sa.Column(
                "schema_version",
                sa.String(length=64),
                nullable=False,
                server_default="legacy",
            )
        )
        batch.add_column(
            sa.Column(
                "is_reviewed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch.add_column(
            sa.Column(
                "is_valid",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )

    op.execute(
        sa.text(
            "UPDATE correction_events "
            "SET document_reference = 'document:' || document_id "
            "WHERE document_reference = 'document:legacy'"
        )
    )
    op.create_index(
        "ix_correction_events_scope_valid_created",
        "correction_events",
        [
            "document_type",
            "field_path",
            "schema_version",
            "is_reviewed",
            "is_valid",
            "created_at",
        ],
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    op.create_table(
        "correction_memories",
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("event_json", sa.JSON(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("is_reviewed", sa.Boolean(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("memory_id", name="pk_correction_memories"),
        sa.UniqueConstraint(
            "fingerprint",
            name="uq_correction_memories_fingerprint",
        ),
    )
    op.create_index(
        "ix_correction_memories_scope_active",
        "correction_memories",
        [
            "document_type",
            "field_path",
            "schema_version",
            "is_reviewed",
            "is_valid",
        ],
    )
    op.create_index(
        "ix_correction_memories_embedding_hnsw",
        "correction_memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "correction_memory_sources",
        sa.Column("source_event_id", sa.String(length=64), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"],
            ["correction_memories.memory_id"],
            name="fk_correction_memory_sources_memory_id_correction_memories",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["correction_events.event_id"],
            name="fk_correction_memory_sources_source_event_id_correction_events",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "source_event_id",
            name="pk_correction_memory_sources",
        ),
    )
    op.create_index(
        "ix_correction_memory_sources_memory_id",
        "correction_memory_sources",
        ["memory_id"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(
            "ix_correction_memory_sources_memory_id",
            table_name="correction_memory_sources",
        )
        op.drop_table("correction_memory_sources")
        op.drop_index(
            "ix_correction_memories_embedding_hnsw",
            table_name="correction_memories",
            postgresql_using="hnsw",
        )
        op.drop_index(
            "ix_correction_memories_scope_active",
            table_name="correction_memories",
        )
        op.drop_table("correction_memories")

    op.drop_index(
        "ix_correction_events_scope_valid_created",
        table_name="correction_events",
    )
    with op.batch_alter_table("correction_events") as batch:
        batch.drop_column("is_valid")
        batch.drop_column("is_reviewed")
        batch.drop_column("schema_version")
        batch.alter_column(
            "image_reference",
            new_column_name="context_reference",
            existing_type=sa.Text(),
            existing_nullable=True,
        )
        batch.drop_column("document_reference")
        batch.drop_column("template_features_json")
        batch.drop_column("vendor_features_json")
        batch.alter_column(
            "correction_reason",
            new_column_name="reason",
            existing_type=sa.Text(),
            existing_nullable=False,
        )
        batch.alter_column(
            "corrected_value_json",
            new_column_name="correct_json",
            existing_type=sa.JSON(),
            existing_nullable=True,
        )
        batch.alter_column(
            "model_value_json",
            new_column_name="incorrect_json",
            existing_type=sa.JSON(),
            existing_nullable=True,
        )
        batch.drop_column("document_type")
    op.create_index(
        "ix_correction_events_checksum_created",
        "correction_events",
        ["document_checksum", "created_at", "event_id"],
    )
