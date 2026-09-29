"""Persist bounded invoice batches, source files, and per-invoice provenance."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0050_invoice_batches"
down_revision: str | None = "20260928_0049_memory_benefit_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_batches",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_hash", name="uq_invoice_batch_key"),
    )
    op.create_index("ix_invoice_batches_tenant_id", "invoice_batches", ["tenant_id"])
    op.create_table(
        "invoice_batch_files",
        sa.Column("file_id", sa.String(64), primary_key=True),
        sa.Column("batch_id", sa.String(64), sa.ForeignKey("invoice_batches.batch_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.document_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(256)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer()),
        sa.Column("groups_json", sa.JSON()),
        sa.Column("error_code", sa.String(128)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_invoice_batch_file_ordinal"),
        sa.UniqueConstraint("batch_id", "document_id", name="uq_invoice_batch_file_document"),
    )
    op.create_index("ix_invoice_batch_files_batch_id", "invoice_batch_files", ["batch_id"])
    op.create_index("ix_invoice_batch_files_tenant_id", "invoice_batch_files", ["tenant_id"])
    op.create_table(
        "invoice_batch_items",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column("batch_id", sa.String(64), sa.ForeignKey("invoice_batches.batch_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("file_id", sa.String(64), sa.ForeignKey("invoice_batch_files.file_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("regions_json", sa.JSON(), nullable=False),
        sa.Column("derived_pages_json", sa.JSON()),
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.document_id", ondelete="RESTRICT")),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("extraction_runs.run_id", ondelete="RESTRICT")),
        sa.Column("run_attempt", sa.Integer(), nullable=False),
        sa.Column("retry_key_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "ordinal", name="uq_invoice_batch_item_ordinal"),
    )
    op.create_index("ix_invoice_batch_items_batch_id", "invoice_batch_items", ["batch_id"])
    op.create_index("ix_invoice_batch_items_tenant_id", "invoice_batch_items", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("invoice_batch_items")
    op.drop_table("invoice_batch_files")
    op.drop_table("invoice_batches")
