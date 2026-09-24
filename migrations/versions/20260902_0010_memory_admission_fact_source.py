"""Add trusted-memory admission fact source.

Revision ID: 20260902_0010
Revises: 20260901_0009
Create Date: 2026-09-02
"""

from collections.abc import Sequence
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0010"
down_revision: str | None = "20260901_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ADMISSION_STATUSES = (
    "'pending', 'approved', 'quarantined', 'rejected', 'suspended', 'invalidated'"
)


def upgrade() -> None:
    op.create_table(
        "memory_admission_decisions",
        sa.Column("decision_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("decided_by", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("assessment_ids_json", sa.JSON(), nullable=False),
        sa.Column("conflict_ids_json", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"status IN ({_ADMISSION_STATUSES})",
            name="ck_memory_admission_decisions_status",
        ),
        sa.CheckConstraint(
            f"previous_status IS NULL OR previous_status IN ({_ADMISSION_STATUSES})",
            name="ck_memory_admission_decisions_previous_status",
        ),
        sa.CheckConstraint(
            "authority IN ('deterministic_policy', 'human_governor')",
            name="ck_memory_admission_decisions_authority",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_memory_admission_decisions_revision",
        ),
        sa.ForeignKeyConstraint(
            ["example_id"],
            ["reviewed_examples.example_id"],
            name="fk_memory_admission_decisions_example_id_reviewed_examples",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "decision_id",
            name="pk_memory_admission_decisions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "example_id",
            "revision",
            name="uq_memory_admission_decisions_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_memory_admission_decisions_idempotency",
        ),
    )
    op.create_index(
        "ix_memory_admission_decisions_tenant_id",
        "memory_admission_decisions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_admission_decisions_example_revision",
        "memory_admission_decisions",
        ["tenant_id", "example_id", "revision"],
    )

    op.create_table(
        "memory_admission_records",
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_decision_id", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"status IN ({_ADMISSION_STATUSES})",
            name="ck_memory_admission_records_status",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_memory_admission_records_revision",
        ),
        sa.ForeignKeyConstraint(
            ["example_id"],
            ["reviewed_examples.example_id"],
            name="fk_memory_admission_records_example_id_reviewed_examples",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["current_decision_id"],
            ["memory_admission_decisions.decision_id"],
            name="fk_memory_admission_records_current_decision_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("example_id", name="pk_memory_admission_records"),
        sa.UniqueConstraint(
            "current_decision_id",
            name="uq_memory_admission_records_current_decision_id",
        ),
    )
    op.create_index(
        "ix_memory_admission_records_tenant_id",
        "memory_admission_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_admission_records_rebuild",
        "memory_admission_records",
        ["tenant_id", "schema_version", "status", "example_id"],
    )

    op.create_table(
        "memory_quality_assessments",
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=False),
        sa.Column("recommendation", sa.String(length=32), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("model_version_id", sa.String(length=64), nullable=True),
        sa.Column("prompt_version_id", sa.String(length=64), nullable=True),
        sa.Column("advisory_only", sa.Boolean(), nullable=False),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('deterministic', 'model_advisory')",
            name="ck_memory_quality_assessments_source",
        ),
        sa.CheckConstraint(
            "recommendation IN ('recommend_approval', 'recommend_quarantine', "
            "'recommend_rejection')",
            name="ck_memory_quality_assessments_recommendation",
        ),
        sa.CheckConstraint(
            "quality_score >= 0 AND quality_score <= 1",
            name="ck_memory_quality_assessments_score",
        ),
        sa.CheckConstraint(
            "advisory_only",
            name="ck_memory_quality_assessments_advisory",
        ),
        sa.CheckConstraint(
            "(source = 'model_advisory' AND model_version_id IS NOT NULL AND "
            "prompt_version_id IS NOT NULL) OR "
            "(source = 'deterministic' AND model_version_id IS NULL AND "
            "prompt_version_id IS NULL)",
            name="ck_memory_quality_assessments_versions",
        ),
        sa.ForeignKeyConstraint(
            ["example_id"],
            ["reviewed_examples.example_id"],
            name="fk_memory_quality_assessments_example_id_reviewed_examples",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.model_version_id"],
            name="fk_memory_quality_assessments_model_version_id_model_versions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["prompt_version_id"],
            ["prompt_versions.prompt_version_id"],
            name="fk_memory_quality_assessments_prompt_version_id_prompt_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "assessment_id",
            name="pk_memory_quality_assessments",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "example_id",
            "source",
            "policy_version",
            "input_fingerprint",
            name="uq_memory_quality_assessments_replay",
        ),
    )
    op.create_index(
        "ix_memory_quality_assessments_tenant_id",
        "memory_quality_assessments",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_quality_assessments_example_time",
        "memory_quality_assessments",
        ["tenant_id", "example_id", "assessed_at"],
    )

    op.create_table(
        "memory_quality_signals",
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("assessment_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=True),
        sa.Column("evidence_references_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_memory_quality_signals_ordinal"),
        sa.CheckConstraint(
            "source IN ('deterministic', 'model_advisory')",
            name="ck_memory_quality_signals_source",
        ),
        sa.CheckConstraint(
            "verdict IN ('passed', 'warning', 'failed')",
            name="ck_memory_quality_signals_verdict",
        ),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_memory_quality_signals_score",
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["memory_quality_assessments.assessment_id"],
            name="fk_memory_quality_signals_assessment_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("signal_id", name="pk_memory_quality_signals"),
        sa.UniqueConstraint(
            "assessment_id",
            "ordinal",
            name="uq_memory_quality_signals_ordinal",
        ),
    )

    op.create_table(
        "memory_conflicts",
        sa.Column("conflict_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_type", sa.String(length=128), nullable=False),
        sa.Column("field_path", sa.Text(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("conflict_type", sa.String(length=128), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("evidence_references_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_decision_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name="ck_memory_conflicts_status",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND resolution_decision_id IS NULL) "
            "OR (status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL AND "
            "resolution_decision_id IS NOT NULL)",
            name="ck_memory_conflicts_resolution",
        ),
        sa.PrimaryKeyConstraint("conflict_id", name="pk_memory_conflicts"),
        sa.UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_memory_conflicts_tenant_fingerprint",
        ),
    )
    op.create_index(
        "ix_memory_conflicts_tenant_id",
        "memory_conflicts",
        ["tenant_id"],
    )
    op.create_index(
        "ix_memory_conflicts_scope_status",
        "memory_conflicts",
        ["tenant_id", "document_type", "field_path", "schema_version", "status"],
    )

    op.create_table(
        "memory_conflict_examples",
        sa.Column("conflict_id", sa.String(length=64), nullable=False),
        sa.Column("example_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conflict_id"],
            ["memory_conflicts.conflict_id"],
            name="fk_memory_conflict_examples_conflict_id_memory_conflicts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["example_id"],
            ["reviewed_examples.example_id"],
            name="fk_memory_conflict_examples_example_id_reviewed_examples",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conflict_id",
            "example_id",
            name="pk_memory_conflict_examples",
        ),
        sa.UniqueConstraint(
            "conflict_id",
            "ordinal",
            name="uq_memory_conflict_examples_ordinal",
        ),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_memory_conflict_examples_ordinal",
        ),
    )
    op.create_index(
        "ix_memory_conflict_examples_lookup",
        "memory_conflict_examples",
        ["tenant_id", "example_id", "conflict_id"],
    )

    op.create_table(
        "reviewer_reliability_snapshots",
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("reviewer_id", sa.String(length=128), nullable=False),
        sa.Column("profile_version", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=128), nullable=False),
        sa.Column("reviewed_fact_count", sa.Integer(), nullable=False),
        sa.Column("approved_fact_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_fact_count", sa.Integer(), nullable=False),
        sa.Column("rejected_fact_count", sa.Integer(), nullable=False),
        sa.Column("conflict_count", sa.Integer(), nullable=False),
        sa.Column("reliability_score", sa.Float(), nullable=False),
        sa.Column("minimum_sample_met", sa.Boolean(), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "reviewed_fact_count >= 0 AND approved_fact_count >= 0 AND "
            "quarantined_fact_count >= 0 AND rejected_fact_count >= 0 AND "
            "conflict_count >= 0",
            name="ck_reviewer_reliability_snapshots_counts",
        ),
        sa.CheckConstraint(
            "approved_fact_count + quarantined_fact_count + rejected_fact_count "
            "<= reviewed_fact_count",
            name="ck_reviewer_reliability_snapshots_outcomes",
        ),
        sa.CheckConstraint(
            "reliability_score >= 0 AND reliability_score <= 1",
            name="ck_reviewer_reliability_snapshots_score",
        ),
        sa.PrimaryKeyConstraint(
            "snapshot_id",
            name="pk_reviewer_reliability_snapshots",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "reviewer_id",
            "profile_version",
            name="uq_reviewer_reliability_snapshots_version",
        ),
    )
    op.create_index(
        "ix_reviewer_reliability_snapshots_tenant_id",
        "reviewer_reliability_snapshots",
        ["tenant_id"],
    )
    op.create_index(
        "ix_reviewer_reliability_snapshots_lookup",
        "reviewer_reliability_snapshots",
        ["tenant_id", "reviewer_id", "calculated_at"],
    )

    _backfill_pending_admissions()


def _backfill_pending_admissions() -> None:
    connection = op.get_bind()
    reviewed_examples = sa.table(
        "reviewed_examples",
        sa.column("example_id", sa.String(length=64)),
        sa.column("tenant_id", sa.String(length=128)),
        sa.column("schema_version", sa.String(length=64)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    rows = connection.execute(
        sa.select(
            reviewed_examples.c.example_id,
            reviewed_examples.c.tenant_id,
            reviewed_examples.c.schema_version,
            reviewed_examples.c.created_at,
        )
    ).all()
    if not rows:
        return

    decisions = sa.table(
        "memory_admission_decisions",
        sa.column("decision_id", sa.String(length=64)),
        sa.column("tenant_id", sa.String(length=128)),
        sa.column("example_id", sa.String(length=64)),
        sa.column("previous_status", sa.String(length=32)),
        sa.column("status", sa.String(length=32)),
        sa.column("authority", sa.String(length=32)),
        sa.column("decided_by", sa.String(length=128)),
        sa.column("reason", sa.Text()),
        sa.column("reason_codes_json", sa.JSON()),
        sa.column("assessment_ids_json", sa.JSON()),
        sa.column("conflict_ids_json", sa.JSON()),
        sa.column("policy_version", sa.String(length=128)),
        sa.column("idempotency_key_hash", sa.String(length=64)),
        sa.column("revision", sa.Integer()),
        sa.column("decided_at", sa.DateTime(timezone=True)),
    )
    records = sa.table(
        "memory_admission_records",
        sa.column("example_id", sa.String(length=64)),
        sa.column("tenant_id", sa.String(length=128)),
        sa.column("schema_version", sa.String(length=64)),
        sa.column("status", sa.String(length=32)),
        sa.column("current_decision_id", sa.String(length=64)),
        sa.column("policy_version", sa.String(length=128)),
        sa.column("revision", sa.Integer()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    decision_values: list[dict[str, object]] = []
    record_values: list[dict[str, object]] = []
    for row in rows:
        identity = f"{row.tenant_id}\0{row.example_id}"
        decision_id = sha256(f"pending\0{identity}".encode("utf-8")).hexdigest()
        idempotency_hash = sha256(f"backfill\0{identity}".encode("utf-8")).hexdigest()
        policy_version = "memory-admission-v1/backfill"
        decided_at = row.created_at
        decision_values.append(
            {
                "decision_id": decision_id,
                "tenant_id": row.tenant_id,
                "example_id": row.example_id,
                "previous_status": None,
                "status": "pending",
                "authority": "deterministic_policy",
                "decided_by": "migration:20260902_0010",
                "reason": "Existing review fact requires explicit memory admission",
                "reason_codes_json": ["legacy_review_requires_admission"],
                "assessment_ids_json": [],
                "conflict_ids_json": [],
                "policy_version": policy_version,
                "idempotency_key_hash": idempotency_hash,
                "revision": 1,
                "decided_at": decided_at,
            }
        )
        record_values.append(
            {
                "example_id": row.example_id,
                "tenant_id": row.tenant_id,
                "schema_version": row.schema_version,
                "status": "pending",
                "current_decision_id": decision_id,
                "policy_version": policy_version,
                "revision": 1,
                "created_at": decided_at,
                "updated_at": decided_at,
            }
        )
    op.bulk_insert(decisions, decision_values)
    op.bulk_insert(records, record_values)
    projections = sa.table(
        "example_index_projections",
        sa.column("status", sa.String(length=32)),
        sa.column("invalidated_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    connection.execute(
        sa.update(projections).values(
            status="invalidated",
            invalidated_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reviewer_reliability_snapshots_lookup",
        table_name="reviewer_reliability_snapshots",
    )
    op.drop_index(
        "ix_reviewer_reliability_snapshots_tenant_id",
        table_name="reviewer_reliability_snapshots",
    )
    op.drop_table("reviewer_reliability_snapshots")
    op.drop_index(
        "ix_memory_conflict_examples_lookup",
        table_name="memory_conflict_examples",
    )
    op.drop_table("memory_conflict_examples")
    op.drop_index("ix_memory_conflicts_scope_status", table_name="memory_conflicts")
    op.drop_index("ix_memory_conflicts_tenant_id", table_name="memory_conflicts")
    op.drop_table("memory_conflicts")
    op.drop_table("memory_quality_signals")
    op.drop_index(
        "ix_memory_quality_assessments_example_time",
        table_name="memory_quality_assessments",
    )
    op.drop_index(
        "ix_memory_quality_assessments_tenant_id",
        table_name="memory_quality_assessments",
    )
    op.drop_table("memory_quality_assessments")
    op.drop_index(
        "ix_memory_admission_records_rebuild",
        table_name="memory_admission_records",
    )
    op.drop_index(
        "ix_memory_admission_records_tenant_id",
        table_name="memory_admission_records",
    )
    op.drop_table("memory_admission_records")
    op.drop_index(
        "ix_memory_admission_decisions_example_revision",
        table_name="memory_admission_decisions",
    )
    op.drop_index(
        "ix_memory_admission_decisions_tenant_id",
        table_name="memory_admission_decisions",
    )
    op.drop_table("memory_admission_decisions")
