"""Add optional remote-training orchestration and governed model registry.

Revision ID: 20260901_0007
Revises: 20260901_0006
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0007"
down_revision: str | None = "20260901_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_runs",
        sa.Column("training_run_key", sa.String(length=64), nullable=False),
        sa.Column("training_run_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("training_dataset_key", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("candidate_model_version_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_model_version", sa.String(length=256), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("remote_job_id", sa.String(length=256), nullable=True),
        sa.Column("run_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('reranker', 'embedding', 'vision_generation')",
            name="ck_training_runs_target_type",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'export_ready', 'submitted', 'queued', 'running', "
            "'canceling', 'succeeded', 'failed', 'canceled', 'unsupported')",
            name="ck_training_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["training_dataset_key"],
            ["training_dataset_versions.dataset_key"],
            name="fk_training_runs_training_dataset_key_training_dataset_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("training_run_key", name="pk_training_runs"),
        sa.UniqueConstraint(
            "tenant_id",
            "training_run_id",
            name="uq_training_runs_tenant_identity",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "candidate_model_version_id",
            name="uq_training_runs_tenant_candidate_model_version",
        ),
    )
    op.create_index("ix_training_runs_tenant_id", "training_runs", ["tenant_id"])
    op.create_index(
        "ix_training_runs_worker_queue",
        "training_runs",
        ["status", "provider", "created_at"],
    )

    op.create_table(
        "model_artifacts",
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("training_run_key", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("artifact_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "stage IN ('registered', 'offline_evaluation', 'shadow', 'canary', "
            "'production', 'rolled_back', 'rejected')",
            name="ck_model_artifacts_stage",
        ),
        sa.ForeignKeyConstraint(
            ["training_run_key"],
            ["training_runs.training_run_key"],
            name="fk_model_artifacts_training_run_key_training_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_model_artifacts_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("artifact_id", name="pk_model_artifacts"),
        sa.UniqueConstraint(
            "tenant_id",
            "training_run_key",
            name="uq_model_artifacts_tenant_training_run",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "model_version_id",
            name="uq_model_artifacts_tenant_model_version",
        ),
    )
    op.create_index("ix_model_artifacts_tenant_id", "model_artifacts", ["tenant_id"])
    op.create_index(
        "ix_model_artifacts_tenant_stage",
        "model_artifacts",
        ["tenant_id", "stage", "is_valid"],
    )

    op.create_table(
        "model_evaluations",
        sa.Column("model_evaluation_key", sa.String(length=64), nullable=False),
        sa.Column("model_evaluation_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("evaluation_run_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_version", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("baseline_model_version_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_model_version_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=256), nullable=False),
        sa.Column("threshold_version", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("evaluation_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "stage IN ('offline_evaluation', 'shadow', 'canary', 'production')",
            name="ck_model_evaluations_stage",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'failed')",
            name="ck_model_evaluations_status",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["model_artifacts.artifact_id"],
            name="fk_model_evaluations_artifact_id_model_artifacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_model_evaluations_candidate_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "model_evaluation_key",
            name="pk_model_evaluations",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "model_evaluation_id",
            name="uq_model_evaluations_tenant_identity",
        ),
    )
    op.create_index(
        "ix_model_evaluations_tenant_id",
        "model_evaluations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_model_evaluations_artifact_stage",
        "model_evaluations",
        ["tenant_id", "artifact_id", "stage", "created_at"],
    )

    op.create_table(
        "model_deployments",
        sa.Column("deployment_key", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("traffic_percentage", sa.Float(), nullable=False),
        sa.Column("previous_deployment_id", sa.String(length=128), nullable=True),
        sa.Column("deployment_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_type IN ('reranker', 'embedding', 'vision_generation')",
            name="ck_model_deployments_target_type",
        ),
        sa.CheckConstraint(
            "stage IN ('shadow', 'canary', 'production')",
            name="ck_model_deployments_stage",
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'active', 'failed', 'superseded', 'rolled_back')",
            name="ck_model_deployments_status",
        ),
        sa.CheckConstraint(
            "traffic_percentage >= 0 AND traffic_percentage <= 100",
            name="ck_model_deployments_traffic",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["model_artifacts.artifact_id"],
            name="fk_model_deployments_artifact_id_model_artifacts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_model_deployments_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("deployment_key", name="pk_model_deployments"),
        sa.UniqueConstraint(
            "tenant_id",
            "deployment_id",
            name="uq_model_deployments_tenant_identity",
        ),
    )
    op.create_index(
        "ix_model_deployments_tenant_id",
        "model_deployments",
        ["tenant_id"],
    )
    op.create_index(
        "uq_model_deployments_tenant_active_production",
        "model_deployments",
        ["tenant_id", "target_type"],
        unique=True,
        postgresql_where=sa.text("stage = 'production' AND status = 'active'"),
        sqlite_where=sa.text("stage = 'production' AND status = 'active'"),
    )

    op.create_table(
        "promotion_decisions",
        sa.Column("decision_key", sa.String(length=64), nullable=False),
        sa.Column("decision_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("artifact_id", sa.String(length=64), nullable=False),
        sa.Column("from_stage", sa.String(length=32), nullable=False),
        sa.Column("to_stage", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=True),
        sa.Column("decision_json", sa.JSON(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected', 'rollback_required')",
            name="ck_promotion_decisions_decision",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["model_artifacts.artifact_id"],
            name="fk_promotion_decisions_artifact_id_model_artifacts",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("decision_key", name="pk_promotion_decisions"),
        sa.UniqueConstraint(
            "tenant_id",
            "decision_id",
            name="uq_promotion_decisions_tenant_identity",
        ),
    )
    op.create_index(
        "ix_promotion_decisions_tenant_id",
        "promotion_decisions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_promotion_decisions_artifact",
        "promotion_decisions",
        ["tenant_id", "artifact_id", "decided_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_promotion_decisions_artifact", table_name="promotion_decisions")
    op.drop_index("ix_promotion_decisions_tenant_id", table_name="promotion_decisions")
    op.drop_table("promotion_decisions")
    op.drop_index(
        "uq_model_deployments_tenant_active_production",
        table_name="model_deployments",
    )
    op.drop_index("ix_model_deployments_tenant_id", table_name="model_deployments")
    op.drop_table("model_deployments")
    op.drop_index(
        "ix_model_evaluations_artifact_stage",
        table_name="model_evaluations",
    )
    op.drop_index("ix_model_evaluations_tenant_id", table_name="model_evaluations")
    op.drop_table("model_evaluations")
    op.drop_index("ix_model_artifacts_tenant_stage", table_name="model_artifacts")
    op.drop_index("ix_model_artifacts_tenant_id", table_name="model_artifacts")
    op.drop_table("model_artifacts")
    op.drop_index("ix_training_runs_worker_queue", table_name="training_runs")
    op.drop_index("ix_training_runs_tenant_id", table_name="training_runs")
    op.drop_table("training_runs")
