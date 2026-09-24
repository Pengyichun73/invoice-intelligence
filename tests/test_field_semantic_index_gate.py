from dataclasses import replace
from datetime import UTC, datetime

import pytest

from invoice_intelligence.application.services.field_semantic_index_projection import (
    FieldSemanticIndexProjectionService,
)
from invoice_intelligence.domain.examples import IndexVersion, ModelVersion
from invoice_intelligence.domain.field_semantics import (
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    FieldSemanticIndexVersionRecord,
)


class _Repository:
    def __init__(self, record: FieldSemanticIndexVersionRecord) -> None:
        self.record = record
        self.activated = False
        self.previous = None
        self.fail_activation = False

    async def get_version(self, tenant_id, index_version):
        return self.record

    async def get_active_version(self, tenant_id, schema_version):
        return self.previous

    async def activate_version(self, tenant_id, index_version):
        if self.fail_activation:
            raise ValueError("registration failed")
        self.activated = True


class _Store:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.versions: list[str] = []

    async def ensure_collection(self, tenant_id, index_version):
        self.calls.append("ensure")

    async def switch_alias(self, tenant_id, index_version):
        self.calls.append("switch")
        self.versions.append(index_version.value)


class _Catalog:
    async def list_definitions(self, tenant_id, *, catalog_version):
        return (FieldSemanticDefinition(
            schema_version="3.0.0", document_type="invoice",
            canonical_field_path="invoice_number", display_name="发票号码",
            description="号码", value_type="string", aliases=(),
            negative_aliases=(), context_anchors=(),
            catalog_version=catalog_version, tenant_scope=tenant_id,
            is_valid=True,
        ),)


class _Verifier:
    def __init__(self) -> None:
        self.expected_sources = None

    async def verify_field_semantics(self, tenant_id, index_version, expected_sources):
        self.expected_sources = expected_sources
        return True


def _record(*, pending: int, indexed: int) -> FieldSemanticIndexVersionRecord:
    return FieldSemanticIndexVersionRecord(
        tenant_id="tenant-a",
        index_version=IndexVersion("semantic-v1"),
        schema_version="3.0.0",
        catalog_version=FieldSemanticCatalogVersion("catalog-v1"),
        dense_model_version=ModelVersion("dense-v1"),
        sparse_model_version=None,
        is_active=False,
        is_valid=True,
        pending_count=pending,
        processing_count=0,
        indexed_count=indexed,
        failed_count=0,
        invalidated_count=0,
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_field_semantic_index_activation_requires_complete_projection() -> None:
    repository = _Repository(_record(pending=1, indexed=0))
    store = _Store()
    service = FieldSemanticIndexProjectionService(
        catalog=object(),
        projection_repository=repository,
        index_store=store,
        dense_embedding_provider=object(),
        sparse_embedding_provider=None,
    )
    with pytest.raises(ValueError, match="not ready"):
        await service.activate_index_version("tenant-a", IndexVersion("semantic-v1"))
    assert store.calls == []
    assert repository.activated is False

    repository.record = _record(pending=0, indexed=19)
    await service.activate_index_version("tenant-a", IndexVersion("semantic-v1"))
    assert store.calls == ["ensure", "switch"]
    assert repository.activated is True


@pytest.mark.asyncio
async def test_field_semantic_alias_restored_when_registration_fails() -> None:
    repository = _Repository(_record(pending=0, indexed=1))
    repository.previous = replace(repository.record, index_version=IndexVersion("previous"))
    repository.fail_activation = True
    store = _Store()
    service = FieldSemanticIndexProjectionService(
        catalog=object(), projection_repository=repository,
        index_store=store, dense_embedding_provider=object(),
        sparse_embedding_provider=None,
    )
    with pytest.raises(ValueError, match="registration failed"):
        await service.activate_index_version("tenant-a", IndexVersion("semantic-v1"))
    assert store.calls == ["ensure", "switch", "switch"]
    assert store.versions == ["semantic-v1", "previous"]


@pytest.mark.asyncio
async def test_field_index_verifies_catalog_source_set_before_activation() -> None:
    repository = _Repository(_record(pending=0, indexed=1))
    store = _Store()
    verifier = _Verifier()
    service = FieldSemanticIndexProjectionService(
        catalog=_Catalog(), projection_repository=repository,
        index_store=store, dense_embedding_provider=object(),
        sparse_embedding_provider=None, integrity_verifier=verifier,
    )
    assert await service.verify_index_version("tenant-a", IndexVersion("semantic-v1"))
    assert len(verifier.expected_sources) == 1
    assert all(len(identifier) == 64 for identifier in verifier.expected_sources)
