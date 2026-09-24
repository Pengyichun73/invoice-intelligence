"""Projection ownership must be fenced after timeout and reassignment."""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.application.services.projection_heartbeat import projection_heartbeat
from invoice_intelligence.config.settings import Settings
from invoice_intelligence.domain.examples import IndexVersion
from invoice_intelligence.infrastructure.persistence.sqlalchemy_examples import (
    SQLAlchemyExampleRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_field_semantic_index import (
    SQLAlchemyFieldSemanticProjectionRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExampleIndexProjectionRow,
    FieldSemanticIndexProjectionRow,
    FieldSemanticIndexVersionRow,
    IndexVersionRow,
)


def _engine() -> sa.Engine:
    return sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.mark.asyncio
async def test_projection_heartbeat_renews_during_remote_work() -> None:
    calls = 0

    async def renew() -> None:
        nonlocal calls
        calls += 1

    async with projection_heartbeat(renew, 0.03):
        await asyncio.sleep(0.1)
    assert calls >= 1


def test_enabled_projection_worker_requires_stable_identity_and_tenants() -> None:
    base = {
        "_env_file": None,
        "correction_memory_backend": "disabled",
        "business_database_url": "sqlite+pysqlite:///.data/test-settings.sqlite",
        "index_projection_worker_enabled": True,
    }
    with pytest.raises(ValidationError, match="requires ID and tenant scope"):
        Settings(**base)
    settings = Settings(
        **base,
        index_projection_worker_id="index-worker-1",
        index_projection_worker_tenant_ids=("tenant-a",),
    )
    assert settings.index_projection_worker_id == "index-worker-1"


@pytest.mark.asyncio
async def test_example_projection_rejects_late_owner() -> None:
    engine = _engine()
    IndexVersionRow.__table__.create(engine)
    ExampleIndexProjectionRow.__table__.create(engine)
    now = datetime.now(UTC)
    version_id = sha256(b"index\0tenant-a\0index-v1").hexdigest()
    projection_id = SQLAlchemyExampleRepository._projection_id(
        "tenant-a", "example-a", version_id,
    )
    with engine.begin() as connection:
        connection.execute(sa.insert(IndexVersionRow), {
            "index_version_id": version_id, "tenant_id": "tenant-a", "version": "index-v1",
            "schema_version": "3.0.0", "dense_model_version_id": "dense-v1",
            "prompt_version_id": "prompt-v1", "is_active": False, "is_valid": True,
            "created_at": now,
        })
        connection.execute(sa.insert(ExampleIndexProjectionRow), {
            "projection_id": projection_id, "tenant_id": "tenant-a", "example_id": "example-a",
            "index_version_id": version_id, "status": "processing", "attempt_count": 1,
            "worker_id": "worker-old", "lease_token": "old-token",
            "lease_expires_at": now - timedelta(seconds=1),
            "processing_started_at": now - timedelta(minutes=5),
            "created_at": now, "updated_at": now,
        })
    repository = SQLAlchemyExampleRepository(engine)
    with pytest.raises(WorkflowPersistenceError, match="no longer owned"):
        await repository.mark_projected(
            "tenant-a", "example-a", IndexVersion("index-v1"), "checksum",
            "worker-old", "old-token",
        )
    with engine.begin() as connection:
        connection.execute(sa.update(ExampleIndexProjectionRow).values(
            worker_id="worker-new", lease_token="new-token",
            lease_expires_at=now + timedelta(minutes=5), attempt_count=2,
        ))
    with pytest.raises(WorkflowPersistenceError, match="no longer owned"):
        await repository.mark_projected(
            "tenant-a", "example-a", IndexVersion("index-v1"), "checksum",
            "worker-old", "old-token",
        )
    await repository.mark_projected(
        "tenant-a", "example-a", IndexVersion("index-v1"), "checksum",
        "worker-new", "new-token",
    )
    with engine.connect() as connection:
        row = connection.execute(sa.select(
            ExampleIndexProjectionRow.status, ExampleIndexProjectionRow.lease_token,
        )).one()
    assert row.status == "indexed"
    assert row.lease_token is None


@pytest.mark.asyncio
async def test_field_semantic_projection_rejects_late_owner_and_renews() -> None:
    engine = _engine()
    FieldSemanticIndexVersionRow.__table__.create(engine)
    FieldSemanticIndexProjectionRow.__table__.create(engine)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(sa.insert(FieldSemanticIndexVersionRow), {
            "index_version_id": "version-id", "tenant_id": "tenant-a", "version": "index-v1",
            "schema_version": "3.0.0", "catalog_version": "catalog-v1",
            "dense_model_version_id": "dense-v1", "is_active": False, "is_valid": True,
            "created_at": now,
        })
        connection.execute(sa.insert(FieldSemanticIndexProjectionRow), {
            "projection_id": "projection-id", "tenant_id": "tenant-a", "semantic_id": "semantic-a",
            "index_version_id": "version-id", "document_type": "invoice",
            "canonical_field_path": "invoice_number", "schema_version": "3.0.0",
            "catalog_version": "catalog-v1", "display_name": "number",
            "description": "number", "value_type": "string", "approved_aliases_json": [],
            "negative_aliases_json": [], "context_anchors_json": [],
            "source_fingerprint": "fingerprint", "status": "processing", "attempt_count": 1,
            "worker_id": "worker-old", "lease_token": "old-token",
            "lease_expires_at": now - timedelta(seconds=1),
            "processing_started_at": now - timedelta(minutes=5),
            "created_at": now, "updated_at": now,
        })
    repository = SQLAlchemyFieldSemanticProjectionRepository(engine)
    with pytest.raises(WorkflowPersistenceError, match="no longer owned"):
        await repository.renew_lease(
            "tenant-a", "semantic-a", IndexVersion("index-v1"),
            "worker-old", "old-token", 60,
        )
    claims = await repository.claim_pending(
        "tenant-a", IndexVersion("index-v1"), 1, "worker-new", 300,
    )
    assert len(claims) == 1
    assert claims[0].worker_id == "worker-new"
    new_token = claims[0].token
    with pytest.raises(WorkflowPersistenceError, match="no longer owned"):
        await repository.mark_projected(
            "tenant-a", "semantic-a", IndexVersion("index-v1"), "checksum",
            "worker-old", "old-token",
        )
    await repository.renew_lease(
        "tenant-a", "semantic-a", IndexVersion("index-v1"),
        "worker-new", new_token, 60,
    )
    await repository.mark_projected(
        "tenant-a", "semantic-a", IndexVersion("index-v1"), "checksum",
        "worker-new", new_token,
    )
    with engine.connect() as connection:
        row = connection.execute(sa.select(
            FieldSemanticIndexProjectionRow.status, FieldSemanticIndexProjectionRow.lease_token,
        )).one()
    assert row.status == "indexed"
    assert row.lease_token is None
