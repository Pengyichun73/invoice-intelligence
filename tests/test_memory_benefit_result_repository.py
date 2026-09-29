"""Paired evidence writes are atomic, tenant-scoped, and value-free."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.ports.memory_effectiveness import PairedBenefitRun
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.infrastructure.persistence.sqlalchemy_memory_benefit_results import (
    SQLAlchemyMemoryBenefitResultRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    MemoryBenefitJudgmentRow,
    MemoryBenefitRunRow,
)


@pytest.mark.asyncio
async def test_atomic_value_free_benefit_write_and_replay() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        MemoryBenefitRunRow.__table__, MemoryBenefitJudgmentRow.__table__,
    ])
    repository = SQLAlchemyMemoryBenefitResultRepository(engine)
    now = datetime.now(UTC)
    run = PairedBenefitRun(
        run_id="run-1", tenant_id="tenant-a", status="completed",
        dataset_digest="a" * 64, schema_version="3.0.0",
        catalog_version="catalog-a", index_version="field-pattern-v1-a",
        model_version="model-a", prompt_version="prompt-a", case_count=1,
        template_group_count=1,
        metrics={"passed": False, "coverage_sufficient": False,
                 "review_reduction_vs_ocr": 0.0, "correct_field_delta": 0,
                 "wrong_auto_passes": 0, "p95_extra_ms": 0.0},
        scenarios=(), blocker_codes=("paired_coverage_below_minimum",),
        created_at=now, completed_at=now,
    )
    fields = {path: {
        "correct": True, "review_required": False, "auto_accepted": True,
        "evidence_page_number": None, "raw_value": "PRIVATE-INVOICE",
    } for path in InvoiceExtraction.model_fields}
    rows = [{
        "case_id": "case-1", "variant": variant,
        "document_checksum": "b" * 64, "template_group": "group-a",
        "elapsed_ms": 100.0, "fields": fields,
    } for variant in ("vision", "vision_ocr", "vision_ocr_memory")]
    await repository.save_completed(run, rows)
    await repository.save_completed(run, rows)
    assert await repository.get_run("tenant-b", "run-1") is None
    assert (await repository.get_run("tenant-a", "run-1")).case_count == 1
    with Session(engine) as session:
        saved = session.scalars(select(MemoryBenefitJudgmentRow)).all()
        assert len(saved) == 3
        assert "PRIVATE-INVOICE" not in str(saved[0].fields_json)
    engine.dispose()
