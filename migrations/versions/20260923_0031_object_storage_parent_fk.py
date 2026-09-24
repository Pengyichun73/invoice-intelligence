"""Enforce derived object parent document integrity.

Revision ID: 20260923_0031_storage_fk
Revises: 20260923_0030_storage
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260923_0031_storage_fk"
down_revision: str | None = "20260923_0030_storage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "fk_stored_objects_parent_document_id_documents",
        "stored_objects",
        "documents",
        ["parent_document_id"],
        ["document_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_stored_objects_parent_document_id_documents",
        "stored_objects",
        type_="foreignkey",
    )
