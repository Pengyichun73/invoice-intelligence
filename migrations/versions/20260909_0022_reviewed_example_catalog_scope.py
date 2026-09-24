"""Bind reviewed examples to Catalog scope and invalidate legacy projections.

Revision ID: 20260909_0022
Revises: 20260909_0021
Create Date: 2026-09-09
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0022"
down_revision: str | None = "20260909_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_CATALOG_VERSION = "legacy-unversioned"
_POLICY_VERSION = "catalog-scope-invalidation-v1"
_REASON = "Legacy reviewed example has no attributable Field Semantic Catalog version"
_INDEX_REASON = "Example index schema predates Catalog and retention scope fields"


def upgrade() -> None:
    op.add_column(
        "reviewed_examples",
        sa.Column("catalog_version", sa.String(length=128), nullable=True),
    )
    connection = op.get_bind()
    now = datetime.now(UTC)
    connection.execute(
        sa.text(
            "UPDATE reviewed_examples SET catalog_version = :catalog_version, "
            "is_valid = false, invalidated_reason = COALESCE(invalidated_reason, :reason), "
            "invalidated_at = COALESCE(invalidated_at, :invalidated_at), "
            "updated_at = :updated_at"
        ),
        {
            "catalog_version": _LEGACY_CATALOG_VERSION,
            "reason": _REASON,
            "invalidated_at": now,
            "updated_at": now,
        },
    )
    _invalidate_admissions(connection, now)
    connection.execute(
        sa.text(
            "UPDATE example_index_projections SET status = 'invalidated', "
            "processing_started_at = NULL, invalidated_at = COALESCE(invalidated_at, "
            ":invalidated_at), updated_at = :updated_at WHERE status <> 'invalidated'"
        ),
        {"invalidated_at": now, "updated_at": now},
    )
    connection.execute(
        sa.text(
            "UPDATE index_versions SET is_active = false, is_valid = false, "
            "invalidated_reason = :reason, retired_at = COALESCE(retired_at, :retired_at), "
            "invalidated_at = COALESCE(invalidated_at, :invalidated_at)"
        ),
        {
            "reason": _INDEX_REASON,
            "retired_at": now,
            "invalidated_at": now,
        },
    )
    with op.batch_alter_table("reviewed_examples") as batch:
        batch.alter_column(
            "catalog_version",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        batch.drop_index("ix_reviewed_examples_scope_active")
        batch.create_index(
            "ix_reviewed_examples_scope_active",
            [
                "tenant_id",
                "document_type",
                "field_path",
                "schema_version",
                "catalog_version",
                "is_reviewed",
                "is_valid",
            ],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("reviewed_examples") as batch:
        batch.drop_index("ix_reviewed_examples_scope_active")
        batch.create_index(
            "ix_reviewed_examples_scope_active",
            [
                "tenant_id",
                "document_type",
                "field_path",
                "schema_version",
                "is_reviewed",
                "is_valid",
            ],
            unique=False,
        )
        batch.drop_column("catalog_version")
    # Fail-closed invalidations and immutable admission decisions are not reversed.


def _invalidate_admissions(connection: sa.Connection, now: datetime) -> None:
    admissions = connection.execute(
        sa.text(
            "SELECT example_id, tenant_id, status, revision "
            "FROM memory_admission_records WHERE status <> 'invalidated'"
        )
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
                "conflict_ids_json, policy_version, idempotency_key_hash, revision, "
                "decided_at) VALUES (:decision_id, :tenant_id, :example_id, "
                ":previous_status, 'invalidated', 'deterministic_policy', "
                "'catalog-scope-migration', :reason, :reason_codes, :assessment_ids, "
                ":conflict_ids, :policy_version, :idempotency_hash, :revision, :decided_at)"
            ),
            {
                "decision_id": decision_id,
                "tenant_id": tenant_id,
                "example_id": example_id,
                "previous_status": str(admission["status"]),
                "reason": _REASON,
                "reason_codes": json.dumps(["memory.catalog_version_unattributed"]),
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
                "WHERE tenant_id = :tenant_id AND example_id = :example_id"
            ),
            {
                "decision_id": decision_id,
                "policy_version": _POLICY_VERSION,
                "revision": revision_number,
                "updated_at": now,
                "tenant_id": tenant_id,
                "example_id": example_id,
            },
        )
