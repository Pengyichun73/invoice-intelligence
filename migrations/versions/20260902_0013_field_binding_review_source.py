"""Add audited sources for human-confirmed field binding aliases.

Revision ID: 20260902_0013
Revises: 20260902_0012
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0013"
down_revision: str | None = "20260902_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("field_semantic_aliases") as batch:
        batch.add_column(sa.Column("source_run_id", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("source_document_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("source_evidence_id", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "source_binding_decision_id",
                sa.String(length=64),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("submission_reason", sa.Text(), nullable=True))
        batch.create_check_constraint(
            "ck_field_semantic_aliases_review_source",
            "(source_run_id IS NULL AND source_document_id IS NULL "
            "AND source_evidence_id IS NULL AND source_binding_decision_id IS NULL "
            "AND submission_reason IS NULL) OR "
            "(source_run_id IS NOT NULL AND source_document_id IS NOT NULL "
            "AND source_evidence_id IS NOT NULL "
            "AND source_binding_decision_id IS NOT NULL "
            "AND submission_reason IS NOT NULL)",
        )
        batch.create_unique_constraint(
            "uq_field_semantic_aliases_review_source",
            [
                "tenant_id",
                "source_run_id",
                "source_evidence_id",
                "source_binding_decision_id",
            ],
        )
        batch.create_index(
            "ix_field_semantic_aliases_review_source",
            [
                "tenant_id",
                "source_run_id",
                "source_document_id",
                "source_evidence_id",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("field_semantic_aliases") as batch:
        batch.drop_index("ix_field_semantic_aliases_review_source")
        batch.drop_constraint(
            "uq_field_semantic_aliases_review_source",
            type_="unique",
        )
        batch.drop_constraint(
            "ck_field_semantic_aliases_review_source",
            type_="check",
        )
        batch.drop_column("submission_reason")
        batch.drop_column("source_binding_decision_id")
        batch.drop_column("source_evidence_id")
        batch.drop_column("source_document_id")
        batch.drop_column("source_run_id")
