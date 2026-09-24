"""Add governed object-storage metadata and phase-one document linkage.

Revision ID: 20260923_0030_storage
Revises: 20260923_0029_training
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0030_storage"
down_revision: str | None = "20260923_0029_training"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stored_objects",
        sa.Column("object_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("parent_document_id", sa.String(36), nullable=True),
        sa.Column("object_kind", sa.String(32), nullable=False),
        sa.Column("stable_key", sa.String(512), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(128), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_stored_objects_revision_positive"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_stored_objects_attempt_nonnegative"),
        sa.CheckConstraint(
            "status IN ('migration_pending','pending','available','delete_pending',"
            "'deleting','deleted','failed')",
            name="ck_stored_objects_status",
        ),
        sa.PrimaryKeyConstraint("object_id", name="pk_stored_objects"),
        sa.UniqueConstraint(
            "tenant_id", "object_kind", "stable_key", name="uq_stored_object_key"
        ),
    )
    op.create_index(
        "ix_stored_objects_lifecycle",
        "stored_objects",
        ["status", "next_attempt_at", "lease_expires_at"],
    )
    op.create_index("ix_stored_objects_tenant_id", "stored_objects", ["tenant_id"])
    op.add_column("documents", sa.Column("original_object_id", sa.String(64), nullable=True))
    op.add_column("documents", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.add_column(
        "documents",
        sa.Column("storage_status", sa.String(32), nullable=False, server_default="available"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO stored_objects (
                object_id, tenant_id, parent_document_id, object_kind, stable_key,
                storage_uri, checksum, media_type, size_bytes, status, revision,
                attempt_count, created_at, updated_at
            )
            SELECT document_id, tenant_id, NULL, 'original', 'legacy/' || document_id,
                   storage_uri, checksum, mime_type, NULL, 'migration_pending', 1,
                   0, created_at, created_at
            FROM documents
            ON CONFLICT (object_id) DO NOTHING
            """
        )
    )
    op.execute("UPDATE documents SET original_object_id = document_id")
    op.create_foreign_key(
        "fk_documents_original_object_id_stored_objects",
        "documents",
        "stored_objects",
        ["original_object_id"],
        ["object_id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_documents_original_object_id", "documents", ["original_object_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_original_object_id", table_name="documents")
    op.drop_constraint(
        "fk_documents_original_object_id_stored_objects", "documents", type_="foreignkey"
    )
    op.drop_column("documents", "storage_status")
    op.drop_column("documents", "size_bytes")
    op.drop_column("documents", "original_object_id")
    op.drop_index("ix_stored_objects_tenant_id", table_name="stored_objects")
    op.drop_index("ix_stored_objects_lifecycle", table_name="stored_objects")
    op.drop_table("stored_objects")
