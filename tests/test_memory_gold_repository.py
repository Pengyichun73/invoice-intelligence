"""Gold annotation facts enforce tenant scope, actor slots, and freeze CAS."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import ResourceConflictError
from invoice_intelligence.infrastructure.persistence.sqlalchemy_memory_gold import (
    SQLAlchemyMemoryGoldRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    DocumentRow,
    MemoryGoldAnnotationRow,
    MemoryGoldCaseRow,
    StoredObjectRow,
)


@pytest.mark.asyncio
async def test_gold_repository_freezes_only_same_tenant_three_person_case() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        DocumentRow.__table__, StoredObjectRow.__table__,
        MemoryGoldCaseRow.__table__, MemoryGoldAnnotationRow.__table__,
    ])
    with Session(engine) as session:
        session.add(DocumentRow(
            document_id="doc-1", tenant_id="tenant-a", storage_uri="isolated://doc-1",
            mime_type="image/png", checksum="a" * 64, original_object_id=None,
            size_bytes=100, storage_status="available", created_at=datetime.now(UTC),
        ))
        session.commit()
    repository = SQLAlchemyMemoryGoldRepository(engine)
    bindings = {
        "schema_version": "3.0.0", "model_version": "model-a",
        "prompt_version": "prompt-a", "catalog_version": "catalog-a",
        "index_version": "field-pattern-v1-a",
    }
    first = await repository.add_annotation(
        "tenant-a", "doc-1", "a" * 64, "group-a", bindings,
        "reviewer-a", "isolated://first", "b" * 64,
    )
    assert len(first.annotations) == 1
    repeated = await repository.add_annotation(
        "tenant-a", "doc-1", "a" * 64, "group-a", bindings,
        "reviewer-a", "isolated://first", "b" * 64,
    )
    assert len(repeated.annotations) == 1
    second = await repository.add_annotation(
        "tenant-a", "doc-1", "a" * 64, "group-a", bindings,
        "reviewer-b", "isolated://second", "c" * 64,
    )
    assert [item.slot for item in second.annotations] == ["first", "second"]
    with pytest.raises(ResourceConflictError):
        await repository.freeze(
            "tenant-a", "doc-1", ("b" * 64, "c" * 64),
            "reviewer-b", {}, "isolated://gold", "d" * 64,
        )
    frozen = await repository.freeze(
        "tenant-a", "doc-1", ("b" * 64, "c" * 64),
        "reviewer-c", {"invoice_number": "first"},
        "isolated://gold", "d" * 64,
    )
    assert frozen.status == "frozen"
    assert frozen.field_choices == {"invoice_number": "first"}
    assert await repository.get_case("tenant-b", "doc-1") is None
    with pytest.raises(ResourceConflictError):
        await repository.freeze(
            "tenant-a", "doc-1", ("b" * 64, "c" * 64),
            "reviewer-d", {}, "isolated://other", "e" * 64,
        )
    engine.dispose()
