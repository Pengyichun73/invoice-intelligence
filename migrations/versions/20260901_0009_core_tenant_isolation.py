"""Add tenant ownership to core business resources and pgvector fallback.

Revision ID: 20260901_0009
Revises: 20260901_0008
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0009"
down_revision: str | None = "20260901_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_TENANT = "__legacy_unassigned__"


def upgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                nullable=False,
                server_default=_LEGACY_TENANT,
            )
        )
    op.create_index("ix_documents_tenant_id", "documents", ["tenant_id"])

    with op.batch_alter_table("extraction_runs") as batch:
        batch.add_column(
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                nullable=False,
                server_default=_LEGACY_TENANT,
            )
        )
    op.execute(
        sa.text(
            "UPDATE extraction_runs SET tenant_id = "
            "(SELECT documents.tenant_id FROM documents "
            "WHERE documents.document_id = extraction_runs.document_id)"
        )
    )
    op.create_index("ix_extraction_runs_tenant_id", "extraction_runs", ["tenant_id"])

    with op.batch_alter_table("documents") as batch:
        batch.alter_column(
            "tenant_id",
            existing_type=sa.String(length=128),
            existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("extraction_runs") as batch:
        batch.alter_column(
            "tenant_id",
            existing_type=sa.String(length=128),
            existing_nullable=False,
            server_default=None,
        )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.drop_index(
        "ix_correction_memories_scope_active",
        table_name="correction_memories",
    )
    op.add_column(
        "correction_memories",
        sa.Column(
            "tenant_id",
            sa.String(length=128),
            nullable=False,
            server_default=_LEGACY_TENANT,
        ),
    )
    op.alter_column(
        "correction_memories",
        "tenant_id",
        existing_type=sa.String(length=128),
        existing_nullable=False,
        server_default=None,
    )
    op.create_index(
        "ix_correction_memories_scope_active",
        "correction_memories",
        [
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
            "is_reviewed",
            "is_valid",
        ],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index(
            "ix_correction_memories_scope_active",
            table_name="correction_memories",
        )
        op.drop_column("correction_memories", "tenant_id")
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

    op.drop_index("ix_extraction_runs_tenant_id", table_name="extraction_runs")
    with op.batch_alter_table("extraction_runs") as batch:
        batch.drop_column("tenant_id")
    op.drop_index("ix_documents_tenant_id", table_name="documents")
    with op.batch_alter_table("documents") as batch:
        batch.drop_column("tenant_id")
