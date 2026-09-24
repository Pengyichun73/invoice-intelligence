"""索引激活前必须比对 PostgreSQL 合格事实与派生投影。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.domain.examples import IndexVersion
from invoice_intelligence.infrastructure.indexing.integrity import (
    SQLAlchemyMilvusIndexIntegrityVerifier,
)
from invoice_intelligence.infrastructure.indexing.milvus_examples import (
    MilvusExampleIndexStore,
    MilvusIndexError,
)
from invoice_intelligence.infrastructure.indexing.milvus_field_semantics import (
    MilvusFieldSemanticIndexStore,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    ExampleIndexProjectionRow,
    FieldSemanticIndexProjectionRow,
    FieldSemanticIndexVersionRow,
    IndexVersionRow,
    MemoryAdmissionRecordRow,
    ReviewedExampleRow,
)


class _ManifestStore:
    def __init__(self, manifest: dict[str, str]) -> None:
        self.manifest = manifest

    async def projection_manifest(self, _tenant_id: str, _version: IndexVersion):
        return self.manifest


def _verifier():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(IndexVersionRow(
            index_version_id="example-version-id", tenant_id="tenant-a",
            version="example-v1", schema_version="3.0.0",
            dense_model_version_id="model-id", prompt_version_id="prompt-id",
            is_active=False, is_valid=True, created_at=now,
        ))
        session.add(ReviewedExampleRow(
            example_id="example-1", tenant_id="tenant-a", replay_key="replay-1",
            semantic_fingerprint="fingerprint-1", source_feedback_id="feedback-1",
            document_id="document-1", run_id="run-1", document_type="invoice",
            field_path="invoice_number", schema_version="3.0.0",
            catalog_version="catalog-1", model_version_id="model-id",
            prompt_version_id="prompt-id", label_type="confirmed_correct",
            evidence_reference_json={"document_reference": "document-1"},
            reviewer_id="reviewer-1", is_reviewed=True, is_valid=True,
            occurrence_count=1, created_at=now, updated_at=now, last_seen_at=now,
        ))
        session.add(MemoryAdmissionRecordRow(
            example_id="example-1", tenant_id="tenant-a", schema_version="3.0.0",
            status="approved", current_decision_id="decision-1",
            policy_version="policy-1", revision=2, attempt_count=0,
            created_at=now, updated_at=now,
        ))
        session.add(ExampleIndexProjectionRow(
            projection_id="projection-1", tenant_id="tenant-a",
            example_id="example-1", index_version_id="example-version-id",
            status="indexed", projection_checksum="checksum-1", attempt_count=1,
            created_at=now, updated_at=now,
        ))
        session.add(FieldSemanticIndexVersionRow(
            index_version_id="field-version-id", tenant_id="tenant-a",
            version="field-v1", schema_version="3.0.0", catalog_version="catalog-1",
            dense_model_version_id="model-id", is_active=False, is_valid=True,
            created_at=now,
        ))
        session.add(FieldSemanticIndexProjectionRow(
            projection_id="field-projection-1", tenant_id="tenant-a",
            semantic_id="semantic-1", index_version_id="field-version-id",
            document_type="invoice", canonical_field_path="invoice_number",
            schema_version="3.0.0", catalog_version="catalog-1",
            display_name="发票号码", description="号码", value_type="string",
            approved_aliases_json=[], negative_aliases_json=[], context_anchors_json=[],
            source_fingerprint="fingerprint-1", status="indexed",
            projection_checksum="field-checksum-1", attempt_count=1,
            created_at=now, updated_at=now,
        ))
    example_store = _ManifestStore({"example-1": "checksum-1"})
    field_store = _ManifestStore({"semantic-1": "field-checksum-1"})
    verifier = SQLAlchemyMilvusIndexIntegrityVerifier(
        engine, example_store=example_store, field_store=field_store
    )
    return verifier, engine, example_store, field_store


@pytest.mark.asyncio
async def test_exact_projection_manifests_are_accepted() -> None:
    verifier, _engine, _examples, _fields = _verifier()
    assert await verifier.verify_examples("tenant-a", IndexVersion("example-v1"))
    assert await verifier.verify_field_semantics(
        "tenant-a", IndexVersion("field-v1"), {"semantic-1": "fingerprint-1"}
    )


@pytest.mark.asyncio
async def test_missing_or_wrong_checksum_blocks_activation() -> None:
    verifier, _engine, examples, fields = _verifier()
    examples.manifest = {}
    fields.manifest = {"semantic-1": "wrong-checksum"}
    assert not await verifier.verify_examples("tenant-a", IndexVersion("example-v1"))
    assert not await verifier.verify_field_semantics(
        "tenant-a", IndexVersion("field-v1"), {"semantic-1": "fingerprint-1"}
    )


@pytest.mark.asyncio
async def test_missing_or_stale_catalog_source_blocks_field_activation() -> None:
    verifier, _engine, _examples, _fields = _verifier()
    assert not await verifier.verify_field_semantics(
        "tenant-a", IndexVersion("field-v1"), {
            "semantic-1": "fingerprint-1", "semantic-2": "fingerprint-2",
        }
    )
    assert not await verifier.verify_field_semantics(
        "tenant-a", IndexVersion("field-v1"), {"semantic-1": "stale-fingerprint"}
    )


@pytest.mark.asyncio
async def test_eligible_case_without_projection_blocks_activation() -> None:
    verifier, engine, _examples, _fields = _verifier()
    with Session(engine) as session, session.begin():
        projection = session.get(ExampleIndexProjectionRow, "projection-1")
        assert projection is not None
        session.delete(projection)
    assert not await verifier.verify_examples("tenant-a", IndexVersion("example-v1"))


class _Iterator:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def next(self):
        result, self.rows = self.rows, []
        return result

    def close(self):
        self.closed = True


class _Client:
    def __init__(self, iterator):
        self.iterator = iterator
        self.options = None

    def query_iterator(self, **options):
        self.options = options
        return self.iterator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("store_type", "identifier_field"),
    [(MilvusExampleIndexStore, "example_id"),
     (MilvusFieldSemanticIndexStore, "semantic_id")],
)
async def test_milvus_manifest_reads_all_rows_with_strong_consistency(
    store_type, identifier_field
) -> None:
    iterator = _Iterator([{
        identifier_field: "item-1", "tenant_id": "tenant-a",
        "index_version": "v1", "projection_checksum": "checksum-1",
    }])
    client = _Client(iterator)
    store = store_type(uri="http://localhost:19530", max_retries=0)
    store._client = client
    store._ensure_collection = AsyncMock()
    manifest = await store.projection_manifest("tenant-a", IndexVersion("v1"))
    assert manifest == {"item-1": "checksum-1"}
    assert client.options["consistency_level"] == "Strong"
    assert iterator.closed


@pytest.mark.asyncio
async def test_milvus_manifest_rejects_wrong_tenant_scope() -> None:
    iterator = _Iterator([{
        "example_id": "item-1", "tenant_id": "tenant-b",
        "index_version": "v1", "projection_checksum": "checksum-1",
    }])
    store = MilvusExampleIndexStore(uri="http://localhost:19530", max_retries=0)
    store._client = _Client(iterator)
    store._ensure_collection = AsyncMock()
    with pytest.raises(MilvusIndexError):
        await store.projection_manifest("tenant-a", IndexVersion("v1"))
    assert iterator.closed
