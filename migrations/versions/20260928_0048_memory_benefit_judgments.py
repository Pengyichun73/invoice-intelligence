"""Persist value-free paired judgments alongside benefit summaries."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0048_memory_benefit_judgments"
down_revision: str | None = "20260928_0047_memory_gold_cases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_benefit_judgments",
        sa.Column("judgment_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey(
            "memory_benefit_runs.run_id", ondelete="RESTRICT"
        ), nullable=False),
        sa.Column("case_id", sa.String(64), nullable=False),
        sa.Column("variant", sa.String(32), nullable=False),
        sa.Column("document_checksum", sa.String(64), nullable=False),
        sa.Column("template_group", sa.String(128), nullable=False),
        sa.Column("elapsed_ms", sa.Float(), nullable=False),
        sa.Column("fields_json", sa.JSON(), nullable=False),
        sa.UniqueConstraint("run_id", "case_id", "variant", name="uq_memory_benefit_judgment"),
        sa.CheckConstraint(
            "variant IN ('vision', 'vision_ocr', 'vision_ocr_memory')",
            name="ck_memory_benefit_judgment_variant",
        ),
        sa.CheckConstraint("elapsed_ms >= 0", name="ck_memory_benefit_judgment_elapsed"),
    )
    op.create_index(
        "ix_memory_benefit_judgments_tenant_id", "memory_benefit_judgments", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_memory_benefit_judgments_tenant_id", table_name="memory_benefit_judgments")
    op.drop_table("memory_benefit_judgments")
