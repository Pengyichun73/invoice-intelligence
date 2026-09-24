import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa


def _load_migration():
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260909_0020_schema_v3_memory_and_ocr_metrics.py"
    )
    spec = importlib.util.spec_from_file_location("schema_v3_memory_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_schema_admission_is_invalidated_and_removed_from_worker_queue(
    monkeypatch,
) -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    reviewed = sa.Table(
        "reviewed_examples",
        metadata,
        sa.Column("example_id", sa.String, primary_key=True),
        sa.Column("schema_version", sa.String, nullable=False),
        sa.Column("is_valid", sa.Boolean, nullable=False),
        sa.Column("invalidated_reason", sa.String),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    admissions = sa.Table(
        "memory_admission_records",
        metadata,
        sa.Column("example_id", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("current_decision_id", sa.String, nullable=False),
        sa.Column("policy_version", sa.String, nullable=False),
        sa.Column("worker_id", sa.String),
        sa.Column("lease_token", sa.String),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    decisions = sa.Table(
        "memory_admission_decisions",
        metadata,
        sa.Column("decision_id", sa.String, primary_key=True),
        sa.Column("tenant_id", sa.String, nullable=False),
        sa.Column("example_id", sa.String, nullable=False),
        sa.Column("previous_status", sa.String),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("authority", sa.String, nullable=False),
        sa.Column("decided_by", sa.String, nullable=False),
        sa.Column("reason", sa.String, nullable=False),
        sa.Column("reason_codes_json", sa.JSON, nullable=False),
        sa.Column("assessment_ids_json", sa.JSON, nullable=False),
        sa.Column("conflict_ids_json", sa.JSON, nullable=False),
        sa.Column("policy_version", sa.String, nullable=False),
        sa.Column("idempotency_key_hash", sa.String, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    projections = sa.Table(
        "example_index_projections",
        metadata,
        sa.Column("projection_id", sa.String, primary_key=True),
        sa.Column("example_id", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("processing_started_at", sa.DateTime(timezone=True)),
        sa.Column("invalidated_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    metadata.create_all(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            reviewed.insert(),
            {
                "example_id": "legacy",
                "schema_version": "1.0.0",
                "is_valid": True,
                "updated_at": now,
            },
        )
        connection.execute(
            admissions.insert(),
            {
                "example_id": "legacy",
                "tenant_id": "tenant-a",
                "status": "pending",
                "revision": 1,
                "current_decision_id": "initial",
                "policy_version": "old",
                "attempt_count": 3,
                "next_attempt_at": now,
                "updated_at": now,
            },
        )
        connection.execute(
            projections.insert(),
            {
                "projection_id": "projection",
                "example_id": "legacy",
                "status": "pending",
                "updated_at": now,
            },
        )
        migration = _load_migration()
        monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
        migration._invalidate_legacy_memory()

        admission = connection.execute(sa.select(admissions)).mappings().one()
        example = connection.execute(sa.select(reviewed)).mappings().one()
        decision = connection.execute(sa.select(decisions)).mappings().one()
        projection = connection.execute(sa.select(projections)).mappings().one()

    assert admission["status"] == "invalidated"
    assert admission["next_attempt_at"] is None
    assert admission["worker_id"] is None
    assert admission["revision"] == 2
    assert example["is_valid"] is False
    assert decision["status"] == "invalidated"
    assert decision["authority"] == "deterministic_policy"
    assert projection["status"] == "invalidated"
