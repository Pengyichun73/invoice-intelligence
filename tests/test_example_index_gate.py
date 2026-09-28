from datetime import UTC, datetime
from hashlib import sha256
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import WorkflowPersistenceError
from invoice_intelligence.application.services.example_index_projection import (
    ExampleIndexProjectionService,
)
from invoice_intelligence.application.services.example_retrieval import (
    HybridExampleRetrievalService,
)
from invoice_intelligence.domain.examples import (
    ExampleCandidate,
    ExampleEvidenceReference,
    ExampleLabelType,
    ExampleScope,
    IndexVersion,
    RetrievalRecallSource,
    RetrievalScore,
    RetrievedExample,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_examples import (
    SQLAlchemyExampleRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ExampleIndexProjectionRow,
    IndexVersionRow,
)


def test_value_blind_projection_removes_prior_values_and_free_text() -> None:
    candidate = ExampleCandidate(
        example_id="example-1",
        scope=ExampleScope(
            tenant_id="tenant-a",
            document_type="invoice",
            field_path="invoice_number",
            schema_version="3.0.0",
            catalog_version="catalog-v1",
        ),
        label_type=ExampleLabelType.CORRECTED,
        redacted_model_value="OLD-ERROR-999",
        redacted_reviewed_value="OLD-TRUTH-999",
        redacted_correction_reason="old value OLD-TRUTH-999",
        vendor_fingerprint=None,
        template_fingerprint="template-1",
        evidence_reference=ExampleEvidenceReference(document_reference="document-1"),
        redacted_index_text="seed",
        redaction_policy_version="v1",
        index_version=IndexVersion("field-pattern-v1-test"),
        last_seen_at=datetime.now(UTC),
    )
    blinded = ExampleIndexProjectionService._value_blind_candidate(candidate)
    text = ExampleIndexProjectionService._build_index_text(blinded)
    assert blinded.redacted_model_value is None
    assert blinded.redacted_reviewed_value is None
    assert blinded.redacted_correction_reason == "reviewed_correction"
    assert "OLD-ERROR-999" not in text
    assert "OLD-TRUTH-999" not in text
    reference = HybridExampleRetrievalService._prompt_reference(
        RetrievedExample(
            candidate=candidate,
            score=RetrievalScore(dense=0.8, sparse=None, fusion=None, rerank=None),
            rank=1,
            recall_sources=(RetrievalRecallSource.DENSE,),
        )
    )
    assert reference.model_value is None
    assert reference.reviewed_value is None
    assert reference.correction_reason == "reviewed_correction"


@pytest.mark.asyncio
async def test_example_alias_switch_requires_verification_and_compensates_db_failure() -> None:
    class Repository:
        async def get_active_version(self, tenant_id):
            return IndexVersion("previous")

        async def activate_version(self, tenant_id, index_version):
            raise WorkflowPersistenceError("registration failed")

    class Store:
        def __init__(self):
            self.switched = []

        async def switch_alias(self, tenant_id, index_version):
            self.switched.append((tenant_id, index_version.value))

    store = Store()
    service = ExampleIndexProjectionService(
        projection_repository=Repository(), admission_repository=object(),
        redactor=object(), index_store=store, dense_embedding_provider=object(),
        sparse_embedding_provider=None, redaction_policy_version="v1",
    )
    service.verify_index_version = AsyncMock(return_value=False)
    with pytest.raises(ValueError, match="not ready"):
        await service.activate_index_version("tenant-a", IndexVersion("target"))
    assert store.switched == []

    service.verify_index_version = AsyncMock(return_value=True)
    with pytest.raises(WorkflowPersistenceError, match="registration failed"):
        await service.activate_index_version("tenant-a", IndexVersion("target"))
    assert store.switched == [("tenant-a", "target"), ("tenant-a", "previous")]


@pytest.mark.asyncio
async def test_example_index_activation_requires_complete_projection() -> None:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    IndexVersionRow.__table__.create(engine)
    ExampleIndexProjectionRow.__table__.create(engine)
    now = datetime.now(UTC)
    index_version_id = sha256(b"index\0tenant-a\0index-v1").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.insert(IndexVersionRow),
            {
                "index_version_id": index_version_id,
                "tenant_id": "tenant-a",
                "version": "index-v1",
                "schema_version": "3.0.0",
                "dense_model_version_id": "dense-v1",
                "prompt_version_id": "prompt-v1",
                "is_active": False,
                "is_valid": True,
                "created_at": now,
            },
        )
        connection.execute(
            sa.insert(ExampleIndexProjectionRow),
            {
                "projection_id": "projection-id",
                "tenant_id": "tenant-a",
                "example_id": "example-id",
                "index_version_id": index_version_id,
                "status": "pending",
                "attempt_count": 0,
                "created_at": now,
                "updated_at": now,
            },
        )

    repository = SQLAlchemyExampleRepository(engine)
    with pytest.raises(WorkflowPersistenceError, match="incomplete projections"):
        await repository.activate_version("tenant-a", IndexVersion("index-v1"))

    with engine.begin() as connection:
        connection.execute(
            sa.update(ExampleIndexProjectionRow).values(
                status="indexed",
                projection_checksum="checksum",
                indexed_at=now,
                updated_at=now,
            )
        )

    await repository.activate_version("tenant-a", IndexVersion("index-v1"))
    with engine.connect() as connection:
        is_active = connection.scalar(sa.select(IndexVersionRow.is_active))
    assert is_active is True
