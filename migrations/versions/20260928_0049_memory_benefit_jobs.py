"""Bind immutable frozen-gold documents to existing Evaluation Job leases."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0049_memory_benefit_jobs"
down_revision: str | None = "20260928_0048_memory_benefit_judgments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_benefit_job_cases",
        sa.Column("job_id", sa.String(64), sa.ForeignKey(
            "evaluation_jobs.job_id", ondelete="RESTRICT"
        ), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(36), sa.ForeignKey(
            "documents.document_id", ondelete="RESTRICT"
        ), nullable=False),
        sa.UniqueConstraint("job_id", "document_id", name="uq_memory_benefit_job_document"),
        sa.CheckConstraint("ordinal >= 0", name="ck_memory_benefit_job_ordinal"),
    )
    op.create_index(
        "ix_memory_benefit_job_cases_tenant_id", "memory_benefit_job_cases", ["tenant_id"]
    )
    with op.batch_alter_table("evaluation_jobs") as batch:
        batch.drop_constraint("ck_evaluation_job_evidence_binding", type_="check")
        batch.create_check_constraint(
            "ck_evaluation_job_evidence_binding",
            "(evidence_class = 'diagnostic_only' AND snapshot_id IS NOT NULL "
            "AND dataset_key IS NULL AND dataset_id IS NULL AND suite IS NULL "
            "AND retrieval_policy_version IS NULL) OR "
            "(evidence_class = 'suite_run' AND snapshot_id IS NULL "
            "AND dataset_key IS NOT NULL AND dataset_id IS NOT NULL AND suite IN "
            "('case_rag', 'trusted_memory_field_binding') "
            "AND retrieval_policy_version IS NOT NULL) OR "
            "(evidence_class = 'memory_benefit' AND snapshot_id IS NULL "
            "AND dataset_key IS NULL AND dataset_id IS NOT NULL AND suite IS NULL "
            "AND catalog_version IS NOT NULL AND retrieval_policy_version IS NULL)",
        )
        batch.drop_constraint("ck_evaluation_job_run_link", type_="check")
        batch.create_check_constraint(
            "ck_evaluation_job_run_link",
            "(evidence_class = 'diagnostic_only' AND evaluation_run_id IS NULL) OR "
            "(evidence_class IN ('suite_run', 'memory_benefit') AND "
            "((status = 'completed' AND evaluation_run_id IS NOT NULL) OR "
            "(status <> 'completed' AND evaluation_run_id IS NULL)))",
        )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text(
        "SELECT count(*) FROM evaluation_jobs WHERE evidence_class = 'memory_benefit'"
    )):
        raise RuntimeError("Memory benefit Jobs must be retained before downgrade")
    with op.batch_alter_table("evaluation_jobs") as batch:
        batch.drop_constraint("ck_evaluation_job_evidence_binding", type_="check")
        batch.create_check_constraint(
            "ck_evaluation_job_evidence_binding",
            "(evidence_class = 'diagnostic_only' AND snapshot_id IS NOT NULL "
            "AND dataset_key IS NULL AND dataset_id IS NULL AND suite IS NULL "
            "AND retrieval_policy_version IS NULL) OR "
            "(evidence_class = 'suite_run' AND snapshot_id IS NULL "
            "AND dataset_key IS NOT NULL AND dataset_id IS NOT NULL AND suite IN "
            "('case_rag', 'trusted_memory_field_binding') "
            "AND retrieval_policy_version IS NOT NULL)",
        )
        batch.drop_constraint("ck_evaluation_job_run_link", type_="check")
        batch.create_check_constraint(
            "ck_evaluation_job_run_link",
            "(evidence_class = 'diagnostic_only' AND evaluation_run_id IS NULL) OR "
            "(evidence_class = 'suite_run' AND "
            "((status = 'completed' AND evaluation_run_id IS NOT NULL) OR "
            "(status <> 'completed' AND evaluation_run_id IS NULL)))",
        )
    op.drop_index("ix_memory_benefit_job_cases_tenant_id", table_name="memory_benefit_job_cases")
    op.drop_table("memory_benefit_job_cases")
