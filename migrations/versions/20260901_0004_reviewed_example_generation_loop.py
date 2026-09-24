"""Add canonical reviewed-example fingerprints and source audit evidence.

Revision ID: 20260901_0004
Revises: 20260901_0003
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0004"
down_revision: str | None = "20260901_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("example_feedback") as batch:
        batch.add_column(
            sa.Column("semantic_fingerprint", sa.String(length=64), nullable=True)
        )
        batch.add_column(sa.Column("evidence_reference_json", sa.JSON(), nullable=True))
    with op.batch_alter_table("reviewed_examples") as batch:
        batch.add_column(
            sa.Column("semantic_fingerprint", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        sa.text(
            "UPDATE reviewed_examples "
            "SET semantic_fingerprint = replay_key, last_seen_at = updated_at"
        )
    )
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                "UPDATE example_feedback AS feedback "
                "SET semantic_fingerprint = reviewed.semantic_fingerprint, "
                "evidence_reference_json = reviewed.evidence_reference_json "
                "FROM reviewed_examples AS reviewed "
                "WHERE reviewed.source_feedback_id = feedback.feedback_id"
            )
        )
    elif dialect == "sqlite":
        op.execute(
            sa.text(
                "UPDATE example_feedback SET "
                "semantic_fingerprint = (SELECT semantic_fingerprint "
                "FROM reviewed_examples WHERE source_feedback_id = feedback_id), "
                "evidence_reference_json = (SELECT evidence_reference_json "
                "FROM reviewed_examples WHERE source_feedback_id = feedback_id)"
            )
        )
    else:
        raise RuntimeError("Unsupported business database dialect")

    with op.batch_alter_table("example_feedback") as batch:
        batch.alter_column("semantic_fingerprint", nullable=False)
        batch.alter_column("evidence_reference_json", nullable=False)
        batch.drop_constraint("ck_example_feedback_corrected_reason", type_="check")
        batch.create_check_constraint(
            "ck_example_feedback_review_reason",
            "label_type NOT IN ('corrected', 'confirmed_incorrect') OR reason IS NOT NULL",
        )
    with op.batch_alter_table("reviewed_examples") as batch:
        batch.alter_column("semantic_fingerprint", nullable=False)
        batch.alter_column("last_seen_at", nullable=False)
        batch.create_unique_constraint(
            "uq_reviewed_examples_tenant_fingerprint",
            ["tenant_id", "semantic_fingerprint"],
        )
        batch.drop_constraint("ck_reviewed_examples_corrected_reason", type_="check")
        batch.create_check_constraint(
            "ck_reviewed_examples_review_reason",
            "label_type NOT IN ('corrected', 'confirmed_incorrect') "
            "OR correction_reason IS NOT NULL",
        )
        batch.create_check_constraint(
            "ck_reviewed_examples_negative_value",
            "label_type != 'confirmed_incorrect' OR reviewed_value_json IS NULL",
        )
        batch.alter_column("occurrence_count", server_default=None)
    op.create_index(
        "ix_example_feedback_tenant_fingerprint",
        "example_feedback",
        ["tenant_id", "semantic_fingerprint"],
    )
    op.create_index(
        "ix_reviewed_examples_tenant_last_seen",
        "reviewed_examples",
        ["tenant_id", "last_seen_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reviewed_examples_tenant_last_seen",
        table_name="reviewed_examples",
    )
    op.drop_index(
        "ix_example_feedback_tenant_fingerprint",
        table_name="example_feedback",
    )
    with op.batch_alter_table("reviewed_examples") as batch:
        batch.drop_constraint("ck_reviewed_examples_negative_value", type_="check")
        batch.drop_constraint("ck_reviewed_examples_review_reason", type_="check")
        batch.create_check_constraint(
            "ck_reviewed_examples_corrected_reason",
            "label_type != 'corrected' OR correction_reason IS NOT NULL",
        )
        batch.drop_constraint(
            "uq_reviewed_examples_tenant_fingerprint",
            type_="unique",
        )
        batch.drop_column("last_seen_at")
        batch.drop_column("occurrence_count")
        batch.drop_column("semantic_fingerprint")
    with op.batch_alter_table("example_feedback") as batch:
        batch.drop_constraint("ck_example_feedback_review_reason", type_="check")
        batch.create_check_constraint(
            "ck_example_feedback_corrected_reason",
            "label_type != 'corrected' OR reason IS NOT NULL",
        )
        batch.drop_column("evidence_reference_json")
        batch.drop_column("semantic_fingerprint")
