"""准入列表按租户和 Run 过滤后再应用游标分页。"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.domain.admission import MemoryAdmissionStatus
from invoice_intelligence.infrastructure.persistence.sqlalchemy_admission import (
    SQLAlchemyMemoryAdmissionRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    MemoryAdmissionRecordRow,
    ReviewedExampleRow,
)


@pytest.mark.asyncio
async def test_admission_run_filter_applies_before_pagination_and_keeps_tenant_scope() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    cases = (
        ("example-0", "tenant-b", "run-a"),
        ("example-1", "tenant-a", "run-b"),
        ("example-2", "tenant-a", "run-a"),
        ("example-3", "tenant-a", "run-a"),
    )
    with Session(engine) as session, session.begin():
        for example_id, tenant_id, run_id in cases:
            session.add(ReviewedExampleRow(
                example_id=example_id, tenant_id=tenant_id, replay_key=example_id,
                semantic_fingerprint=example_id, source_feedback_id=example_id,
                document_id="document-1", run_id=run_id, document_type="invoice",
                field_path="invoice_number", schema_version="3.0.0",
                catalog_version="catalog-1", model_version_id="model-1",
                prompt_version_id="prompt-1", label_type="confirmed_correct",
                model_value_json=None, reviewed_value_json=None,
                evidence_reference_json={"document_reference": "document-1"},
                reviewer_id="reviewer-1", is_reviewed=True, is_valid=True,
                occurrence_count=1, created_at=now, updated_at=now, last_seen_at=now,
            ))
            session.add(MemoryAdmissionRecordRow(
                example_id=example_id, tenant_id=tenant_id, schema_version="3.0.0",
                status="pending", current_decision_id=example_id,
                policy_version="policy-1", revision=1, attempt_count=0,
                created_at=now, updated_at=now,
            ))

    repository = SQLAlchemyMemoryAdmissionRepository(engine)
    first = await repository.list_by_status(
        "tenant-a", (MemoryAdmissionStatus.PENDING,), limit=1, run_id="run-a"
    )
    second = await repository.list_by_status(
        "tenant-a", (MemoryAdmissionStatus.PENDING,), limit=1,
        after_example_id=first[0].example_id, run_id="run-a",
    )

    assert [item.example_id for item in first] == ["example-2"]
    assert [item.example_id for item in second] == ["example-3"]
    assert await repository.list_by_status(
        "tenant-a", (MemoryAdmissionStatus.PENDING,), limit=10, run_id="run-b"
    ) == (await repository.get("tenant-a", "example-1"),)
    engine.dispose()
