"""Add private-object gold annotation and adjudication facts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0047_memory_gold_cases"
down_revision: str | None = "20260928_0046_memory_benefit_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_gold_cases",
        sa.Column("document_id", sa.String(36), sa.ForeignKey("documents.document_id",
                  ondelete="RESTRICT"), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_checksum", sa.String(64), nullable=False),
        sa.Column("template_group", sa.String(128), nullable=False),
        sa.Column("versions_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("gold_ref", sa.Text(), nullable=True),
        sa.Column("gold_checksum", sa.String(64), nullable=True),
        sa.Column("adjudicator_id", sa.String(128), nullable=True),
        sa.Column("field_choices_json", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'frozen')", name="ck_memory_gold_case_status"),
        sa.CheckConstraint(
            "(status = 'open' AND gold_ref IS NULL AND gold_checksum IS NULL "
            "AND adjudicator_id IS NULL AND field_choices_json IS NULL AND frozen_at IS NULL) "
            "OR (status = 'frozen' AND gold_ref IS NOT NULL AND gold_checksum IS NOT NULL "
            "AND adjudicator_id IS NOT NULL AND field_choices_json IS NOT NULL "
            "AND frozen_at IS NOT NULL)",
            name="ck_memory_gold_case_freeze",
        ),
    )
    op.create_index("ix_memory_gold_cases_tenant_id", "memory_gold_cases", ["tenant_id"])
    op.create_table(
        "memory_gold_annotations",
        sa.Column("annotation_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey(
            "memory_gold_cases.document_id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("slot", sa.String(8), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("object_ref", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "document_id", "slot", name="uq_memory_gold_slot"),
        sa.UniqueConstraint("tenant_id", "document_id", "actor_id", name="uq_memory_gold_actor"),
        sa.CheckConstraint("slot IN ('first', 'second')", name="ck_memory_gold_slot"),
    )
    op.create_index(
        "ix_memory_gold_annotations_tenant_id", "memory_gold_annotations", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_gold_annotations_tenant_id", table_name="memory_gold_annotations")
    op.drop_table("memory_gold_annotations")
    op.drop_index("ix_memory_gold_cases_tenant_id", table_name="memory_gold_cases")
    op.drop_table("memory_gold_cases")
