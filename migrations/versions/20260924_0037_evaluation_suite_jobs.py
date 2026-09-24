"""Bind real Suite evaluation jobs to frozen PostgreSQL datasets.

Revision ID: 20260924_0037_evaluation_suite_jobs
Revises: 20260924_0036_conflict_reevaluation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_0037_evaluation_suite_jobs"
down_revision: str | None = "20260924_0036_conflict_reevaluation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("evaluation_jobs") as batch:
        batch.alter_column("snapshot_id", existing_type=sa.String(64), nullable=True)
        batch.add_column(sa.Column("dataset_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("dataset_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column(
            "evidence_class", sa.String(32), nullable=False,
            server_default="diagnostic_only",
        ))
        batch.add_column(sa.Column("suite", sa.String(64), nullable=True))
        batch.add_column(sa.Column("retrieval_policy_version", sa.String(128), nullable=True))
        batch.add_column(sa.Column("catalog_version", sa.String(128), nullable=True))
        batch.add_column(sa.Column("admission_policy_version", sa.String(128), nullable=True))
        batch.add_column(sa.Column("field_binding_policy_version", sa.String(128), nullable=True))
        batch.create_foreign_key(
            "fk_evaluation_jobs_dataset_key", "evaluation_datasets",
            ["dataset_key"], ["dataset_key"], ondelete="RESTRICT",
        )
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


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text(
        "SELECT count(*) FROM evaluation_jobs WHERE evidence_class = 'suite_run'"
    )):
        raise RuntimeError("Suite evaluation jobs must be retained before downgrade")
    with op.batch_alter_table("evaluation_jobs") as batch:
        batch.drop_constraint("ck_evaluation_job_evidence_binding", type_="check")
        batch.drop_constraint("fk_evaluation_jobs_dataset_key", type_="foreignkey")
        batch.drop_column("field_binding_policy_version")
        batch.drop_column("admission_policy_version")
        batch.drop_column("catalog_version")
        batch.drop_column("retrieval_policy_version")
        batch.drop_column("suite")
        batch.drop_column("evidence_class")
        batch.drop_column("dataset_id")
        batch.drop_column("dataset_key")
        batch.alter_column("snapshot_id", existing_type=sa.String(64), nullable=False)
