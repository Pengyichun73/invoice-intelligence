"""Frozen-gold Job ownership and completion use the existing fenced queue."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import ResourceConflictError
from invoice_intelligence.domain.evaluation_jobs import EvaluationJob, EvaluationJobStatus
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation_jobs import (
    SQLAlchemyEvaluationJobRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    EvaluationJobReportRow,
    EvaluationJobRow,
    MemoryBenefitJobCaseRow,
    MemoryBenefitRunRow,
)


@pytest.mark.asyncio
async def test_benefit_job_replay_scope_and_fenced_completion() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        EvaluationJobRow.__table__, MemoryBenefitJobCaseRow.__table__,
        MemoryBenefitRunRow.__table__, EvaluationJobReportRow.__table__,
    ])
    repository = SQLAlchemyEvaluationJobRepository(engine)
    now = datetime.now(UTC)
    job = EvaluationJob(
        job_id="benefit-job-1", tenant_id="tenant-a", snapshot_id=None,
        dataset_id="gold-digest", dataset_version="a" * 64,
        schema_version="3.0.0", catalog_version="catalog-v1",
        index_version="field-pattern-v1-test", model_version="model-v1",
        prompt_version="prompt-v1", threshold_version="gate-v1",
        status=EvaluationJobStatus.PENDING, attempt_count=0,
        next_attempt_at=None, lease_expires_at=None, worker_id=None,
        lease_token=None, failure_code=None, report=None,
        created_at=now, updated_at=now, evidence_class="memory_benefit",
    )
    try:
        created = await repository.create_memory_benefit_job(
            job, "request-a", "key-a", ("document-1",),
        )
        replay = await repository.create_memory_benefit_job(
            job, "request-a", "key-a", ("document-1",),
        )
        assert replay.job_id == created.job_id
        assert await repository.get_memory_benefit_documents("tenant-b", job.job_id) == ()
        assert await repository.get_memory_benefit_documents("tenant-a", job.job_id) == (
            "document-1",
        )
        with pytest.raises(ResourceConflictError, match="Idempotency key"):
            await repository.create_memory_benefit_job(
                job, "request-b", "key-a", ("document-1",),
            )
        claimed = await repository.claim("worker-a", 300, 3)
        assert claimed is not None
        with Session(engine) as session:
            session.add(MemoryBenefitRunRow(
                run_id=job.job_id, tenant_id=job.tenant_id, status="completed",
                dataset_digest=job.dataset_version, schema_version=job.schema_version,
                catalog_version=job.catalog_version, index_version=job.index_version,
                model_version=job.model_version, prompt_version=job.prompt_version,
                case_count=1, template_group_count=1,
                metrics_json={"coverage_sufficient": False, "passed": False},
                scenarios_json=[], blocker_codes_json=["paired_coverage_below_minimum"],
                created_at=now, completed_at=now,
            ))
            session.commit()
        await repository.complete_memory_benefit(claimed, job.job_id)
        completed = await repository.get_job("tenant-a", job.job_id)
        assert completed is not None
        assert completed.status is EvaluationJobStatus.COMPLETED
        assert completed.evaluation_run_id == job.job_id
        assert await repository.get_job("tenant-b", job.job_id) is None
        with pytest.raises(ResourceConflictError, match="lease"):
            await repository.complete_memory_benefit(claimed, job.job_id)
    finally:
        engine.dispose()
