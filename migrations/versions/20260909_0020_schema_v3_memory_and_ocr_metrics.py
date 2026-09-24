"""Invalidate legacy memory and persist sensitive-value-free OCR metrics.

Revision ID: 20260909_0020
Revises: 20260904_0019
Create Date: 2026-09-09
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0020"
down_revision: str | None = "20260904_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_SCHEMA_VERSION = "3.0.0"
_POLICY_VERSION = "schema-v3-memory-invalidation-v1"
_REASON = "Legacy memory is incompatible with active InvoiceExtraction Schema 3.0.0"


def upgrade() -> None:
    _create_metric_tables()
    _invalidate_legacy_memory()


def downgrade() -> None:
    op.drop_index(
        "ix_ocr_comparison_metric_events_created",
        table_name="ocr_comparison_metric_events",
    )
    op.drop_table("ocr_comparison_metric_events")
    op.drop_index(
        "ix_ocr_page_metric_events_provider_event_id",
        table_name="ocr_page_metric_events",
    )
    op.drop_table("ocr_page_metric_events")
    op.drop_index(
        "ix_ocr_provider_metric_events_provider_created",
        table_name="ocr_provider_metric_events",
    )
    op.drop_index(
        "ix_ocr_provider_metric_events_trace_id",
        table_name="ocr_provider_metric_events",
    )
    op.drop_table("ocr_provider_metric_events")
    # Legacy examples remain invalidated: restoring obsolete Schema facts would be unsafe.


def _create_metric_tables() -> None:
    op.create_table(
        "ocr_provider_metric_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("trace_id", sa.String(length=64), nullable=False),
        sa.Column("provider_name", sa.String(length=128), nullable=False),
        sa.Column("provider_version", sa.String(length=128), nullable=False),
        sa.Column("model_version", sa.String(length=256), nullable=False),
        sa.Column("config_version", sa.String(length=128), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("timeout_count", sa.Integer(), nullable=False),
        sa.Column("circuit_open_count", sa.Integer(), nullable=False),
        sa.Column("schema_error_count", sa.Integer(), nullable=False),
        sa.Column("other_error_count", sa.Integer(), nullable=False),
        sa.Column("text_box_count", sa.Integer(), nullable=False),
        sa.Column("empty_rate_numerator", sa.Integer(), nullable=False),
        sa.Column("empty_rate_denominator", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "page_count >= 0 AND latency_ms >= 0 AND success_count >= 0 "
            "AND timeout_count >= 0 AND circuit_open_count >= 0 "
            "AND schema_error_count >= 0 AND other_error_count >= 0 "
            "AND text_box_count >= 0 AND empty_rate_numerator >= 0 "
            "AND empty_rate_denominator >= 0",
            name="ck_ocr_provider_metric_events_counts",
        ),
    )
    op.create_index(
        "ix_ocr_provider_metric_events_trace_id",
        "ocr_provider_metric_events",
        ["trace_id"],
    )
    op.create_index(
        "ix_ocr_provider_metric_events_provider_created",
        "ocr_provider_metric_events",
        ["provider_name", "created_at"],
    )
    op.create_table(
        "ocr_page_metric_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "provider_event_id",
            sa.String(length=64),
            sa.ForeignKey("ocr_provider_metric_events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(length=64), nullable=False),
        sa.Column("text_box_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "page_number > 0 AND latency_ms >= 0 AND text_box_count >= 0",
            name="ck_ocr_page_metric_events_values",
        ),
    )
    op.create_index(
        "ix_ocr_page_metric_events_provider_event_id",
        "ocr_page_metric_events",
        ["provider_event_id"],
    )
    op.create_table(
        "ocr_comparison_metric_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("trace_ids_json", sa.JSON(), nullable=False),
        sa.Column("raw_observation_count", sa.Integer(), nullable=False),
        sa.Column("bound_field_count", sa.Integer(), nullable=False),
        sa.Column("corroborated_count", sa.Integer(), nullable=False),
        sa.Column("conflicting_count", sa.Integer(), nullable=False),
        sa.Column("ocr_only_count", sa.Integer(), nullable=False),
        sa.Column("vision_only_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_count", sa.Integer(), nullable=False),
        sa.Column("unavailable_count", sa.Integer(), nullable=False),
        sa.Column("conflict_review_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "raw_observation_count >= 0 AND bound_field_count >= 0 "
            "AND corroborated_count >= 0 AND conflicting_count >= 0 "
            "AND ocr_only_count >= 0 AND vision_only_count >= 0 "
            "AND unresolved_count >= 0 AND unavailable_count >= 0",
            name="ck_ocr_comparison_metric_events_counts",
        ),
    )
    op.create_index(
        "ix_ocr_comparison_metric_events_created",
        "ocr_comparison_metric_events",
        ["created_at"],
    )


def _invalidate_legacy_memory() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC)
    admissions = connection.execute(
        sa.text(
            "SELECT admission.example_id, admission.tenant_id, admission.status, "
            "admission.revision "
            "FROM memory_admission_records AS admission "
            "JOIN reviewed_examples AS example ON example.example_id = admission.example_id "
            "WHERE example.schema_version <> :schema_version "
            "AND admission.status <> 'invalidated'"
        ),
        {"schema_version": _ACTIVE_SCHEMA_VERSION},
    ).mappings().all()
    for admission in admissions:
        example_id = str(admission["example_id"])
        tenant_id = str(admission["tenant_id"])
        revision_number = int(admission["revision"]) + 1
        seed = f"{tenant_id}:{example_id}:{revision_number}:{_POLICY_VERSION}"
        decision_id = sha256(f"decision:{seed}".encode()).hexdigest()
        idempotency_hash = sha256(f"idempotency:{seed}".encode()).hexdigest()
        connection.execute(
            sa.text(
                "INSERT INTO memory_admission_decisions "
                "(decision_id, tenant_id, example_id, previous_status, status, authority, "
                "decided_by, reason, reason_codes_json, assessment_ids_json, "
                "conflict_ids_json, policy_version, idempotency_key_hash, revision, decided_at) "
                "VALUES (:decision_id, :tenant_id, :example_id, :previous_status, "
                "'invalidated', 'deterministic_policy', 'schema-migration-3.0.0', :reason, "
                ":reason_codes, :assessment_ids, :conflict_ids, :policy_version, "
                ":idempotency_hash, :revision, :decided_at)"
            ),
            {
                "decision_id": decision_id,
                "tenant_id": tenant_id,
                "example_id": example_id,
                "previous_status": str(admission["status"]),
                "reason": _REASON,
                "reason_codes": json.dumps(["memory.schema_version_incompatible"]),
                "assessment_ids": json.dumps([]),
                "conflict_ids": json.dumps([]),
                "policy_version": _POLICY_VERSION,
                "idempotency_hash": idempotency_hash,
                "revision": revision_number,
                "decided_at": now,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE memory_admission_records SET status = 'invalidated', "
                "current_decision_id = :decision_id, policy_version = :policy_version, "
                "revision = :revision, worker_id = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, attempt_count = 0, next_attempt_at = NULL, "
                "last_error_code = NULL, last_error_at = NULL, updated_at = :updated_at "
                "WHERE example_id = :example_id AND tenant_id = :tenant_id"
            ),
            {
                "decision_id": decision_id,
                "policy_version": _POLICY_VERSION,
                "revision": revision_number,
                "updated_at": now,
                "example_id": example_id,
                "tenant_id": tenant_id,
            },
        )
    connection.execute(
        sa.text(
            "UPDATE reviewed_examples SET is_valid = false, "
            "invalidated_reason = :reason, invalidated_at = :invalidated_at, "
            "updated_at = :updated_at WHERE schema_version <> :schema_version AND is_valid"
        ),
        {
            "reason": _REASON,
            "invalidated_at": now,
            "updated_at": now,
            "schema_version": _ACTIVE_SCHEMA_VERSION,
        },
    )
    connection.execute(
        sa.text(
            "UPDATE example_index_projections SET status = 'invalidated', "
            "processing_started_at = NULL, invalidated_at = :invalidated_at, "
            "updated_at = :updated_at WHERE example_id IN "
            "(SELECT example_id FROM reviewed_examples WHERE schema_version <> :schema_version) "
            "AND status <> 'invalidated'"
        ),
        {
            "invalidated_at": now,
            "updated_at": now,
            "schema_version": _ACTIVE_SCHEMA_VERSION,
        },
    )
