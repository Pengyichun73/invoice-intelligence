"""Create business persistence schema.

Revision ID: 20260901_0001
Revises: None
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("storage_uri", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id", name="pk_documents"),
    )
    op.create_index("ix_documents_checksum", "documents", ["checksum"])

    op.create_table(
        "extraction_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("validation_route", sa.String(length=32), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_extraction_runs_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_extraction_runs"),
        sa.UniqueConstraint("thread_id", name="uq_extraction_runs_thread_id"),
    )
    op.create_index(
        "ix_extraction_runs_document_id",
        "extraction_runs",
        ["document_id"],
    )
    op.create_index("ix_extraction_runs_status", "extraction_runs", ["status"])

    op.create_table(
        "extraction_results",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_extraction_results_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.run_id"],
            name="fk_extraction_results_run_id_extraction_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_extraction_results"),
    )
    op.create_index(
        "ix_extraction_results_document_id",
        "extraction_results",
        ["document_id"],
    )

    op.create_table(
        "review_tasks",
        sa.Column("review_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.run_id"],
            name="fk_review_tasks_run_id_extraction_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", name="pk_review_tasks"),
        sa.UniqueConstraint("run_id", name="uq_review_tasks_run_id"),
    )
    op.create_index("ix_review_tasks_status", "review_tasks", ["status"])

    op.create_table(
        "human_corrections",
        sa.Column("correction_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("correction_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.run_id"],
            name="fk_human_corrections_run_id_extraction_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("correction_id", name="pk_human_corrections"),
    )
    op.create_index(
        "ix_human_corrections_run_id",
        "human_corrections",
        ["run_id"],
    )

    op.create_table(
        "correction_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("document_checksum", sa.String(length=64), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("incorrect_json", sa.JSON(), nullable=True),
        sa.Column("correct_json", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context_reference", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.document_id"],
            name="fk_correction_events_document_id_documents",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["extraction_runs.run_id"],
            name="fk_correction_events_run_id_extraction_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_correction_events"),
    )
    op.create_index(
        "ix_correction_events_checksum_created",
        "correction_events",
        ["document_checksum", "created_at", "event_id"],
    )
    op.create_index(
        "ix_correction_events_run_id",
        "correction_events",
        ["run_id"],
    )

    op.create_table(
        "idempotency_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_idempotency_requests"),
        sa.UniqueConstraint(
            "operation",
            "idempotency_key",
            name="uq_idempotency_requests_operation_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_requests")
    op.drop_index("ix_correction_events_run_id", table_name="correction_events")
    op.drop_index(
        "ix_correction_events_checksum_created",
        table_name="correction_events",
    )
    op.drop_table("correction_events")
    op.drop_index("ix_human_corrections_run_id", table_name="human_corrections")
    op.drop_table("human_corrections")
    op.drop_index("ix_review_tasks_status", table_name="review_tasks")
    op.drop_table("review_tasks")
    op.drop_index(
        "ix_extraction_results_document_id",
        table_name="extraction_results",
    )
    op.drop_table("extraction_results")
    op.drop_index("ix_extraction_runs_status", table_name="extraction_runs")
    op.drop_index("ix_extraction_runs_document_id", table_name="extraction_runs")
    op.drop_table("extraction_runs")
    op.drop_index("ix_documents_checksum", table_name="documents")
    op.drop_table("documents")
