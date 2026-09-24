from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.services.evaluation_jobs import (
    CreateEvaluationJobCommand,
    CreateEvaluationScheduleCommand,
    CreateSnapshotCommand,
    EvaluationJobService,
)
from invoice_intelligence.domain.evaluation_jobs import SnapshotCase
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation_jobs import (
    SQLAlchemyEvaluationJobRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    EvaluationJobReportRow,
    EvaluationJobRow,
    EvaluationScheduleRow,
    EvaluationSnapshotRow,
)


def _engine() -> sa.Engine:
    return sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _repository() -> SQLAlchemyEvaluationJobRepository:
    engine = _engine()
    Base.metadata.create_all(
        engine,
        tables=[
            EvaluationSnapshotRow.__table__,
            EvaluationJobRow.__table__,
            EvaluationScheduleRow.__table__,
            EvaluationJobReportRow.__table__,
        ],
    )
    return SQLAlchemyEvaluationJobRepository(engine)


def _context(tenant_id: str) -> TrustedTenantContext:
    return TrustedTenantContext(tenant_id=tenant_id, actor_id="reviewer-a", permissions=frozenset())


def _case(case_id: str = "case-1") -> SnapshotCase:
    return SnapshotCase(
        case_id=case_id,
        field_path="invoice_number",
        expected_present=True,
        predicted_present=True,
        field_correct=True,
        evidence_covered=True,
        amount_absolute_error=None,
        review_required=False,
        negative_false_recall=False,
        ocr_vision_consistent=True,
    )


async def _snapshot(service: EvaluationJobService, tenant_id: str = "tenant-a") -> str:
    snapshot = await service.create_snapshot(
        _context(tenant_id),
        CreateSnapshotCommand(
            dataset_version="dataset-v1",
            schema_version="3.0.0",
            cases=(_case(),),
        ),
        idempotency_key=f"snapshot-{tenant_id}",
    )
    return snapshot.snapshot_id


def _job_command(snapshot_id: str) -> CreateEvaluationJobCommand:
    return CreateEvaluationJobCommand(
        snapshot_id=snapshot_id,
        dataset_version="dataset-v1",
        schema_version="3.0.0",
        index_version="index-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
        threshold_version="threshold-v1",
    )


@pytest.mark.asyncio
async def test_snapshot_version_is_immutable_and_tenant_scoped() -> None:
    service = EvaluationJobService(_repository())
    await _snapshot(service)
    with pytest.raises(ResourceConflictError, match="immutable"):
        await service.create_snapshot(
            _context("tenant-a"),
            CreateSnapshotCommand(
                dataset_version="dataset-v1",
                schema_version="3.0.0",
                cases=(_case("different-case"),),
            ),
            idempotency_key="different-key",
        )
    with pytest.raises(ResourceNotFoundError):
        await service.get_snapshot(_context("tenant-b"), "missing")


@pytest.mark.asyncio
async def test_job_idempotency_replay_and_semantic_conflict() -> None:
    service = EvaluationJobService(_repository())
    snapshot_id = await _snapshot(service)
    first = await service.create_job(
        _context("tenant-a"), _job_command(snapshot_id), idempotency_key="job-key"
    )
    replay = await service.create_job(
        _context("tenant-a"), _job_command(snapshot_id), idempotency_key="job-key"
    )
    assert replay.job_id == first.job_id
    changed = replace(_job_command(snapshot_id), index_version="index-v2")
    with pytest.raises(ResourceConflictError, match="Idempotency key"):
        await service.create_job(_context("tenant-a"), changed, idempotency_key="job-key")


@pytest.mark.asyncio
async def test_job_cross_tenant_read_is_not_visible() -> None:
    service = EvaluationJobService(_repository())
    snapshot_id = await _snapshot(service)
    job = await service.create_job(
        _context("tenant-a"), _job_command(snapshot_id), idempotency_key="job-key"
    )
    with pytest.raises(ResourceNotFoundError):
        await service.get_job(_context("tenant-b"), job.job_id)
    assert await service.list_jobs(_context("tenant-b"), limit=20, offset=0) == ()


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed_and_late_worker_is_fenced() -> None:
    repository = _repository()
    service = EvaluationJobService(repository)
    snapshot_id = await _snapshot(service)
    job = await service.create_job(
        _context("tenant-a"), _job_command(snapshot_id), idempotency_key="job-key"
    )
    claimed = await repository.claim("worker-old", 1, 3)
    assert claimed is not None
    with repository._sessions.begin() as session:
        session.execute(
            sa.update(EvaluationJobRow)
            .where(EvaluationJobRow.job_id == job.job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    reclaimed = await repository.claim("worker-new", 300, 3)
    assert reclaimed is not None
    assert reclaimed.worker_id == "worker-new"
    with pytest.raises(ResourceConflictError):
        await repository.complete(claimed, {"field_accuracy": 1.0})


@pytest.mark.asyncio
async def test_retry_exhaustion_quarantines_job_and_report_is_aggregate_only() -> None:
    repository = _repository()
    service = EvaluationJobService(repository, max_attempts=1)
    snapshot_id = await _snapshot(service)
    await service.create_job(
        _context("tenant-a"), _job_command(snapshot_id), idempotency_key="job-key"
    )
    claimed = await repository.claim("worker-a", 300, 1)
    assert claimed is not None
    await repository.fail(claimed, "evaluation.provider_failure", retry=True, max_attempts=1)
    quarantined = await service.get_job(_context("tenant-a"), claimed.job_id)
    assert quarantined.status.value == "quarantined"
    assert quarantined.failure_code == "evaluation.provider_failure"
    await service.execute_one("worker-b")
    assert quarantined.report is None


@pytest.mark.asyncio
async def test_scheduler_only_enqueues_due_job_and_is_idempotent() -> None:
    repository = _repository()
    service = EvaluationJobService(repository)
    snapshot_id = await _snapshot(service)
    schedule = await service.create_schedule(
        _context("tenant-a"),
        CreateEvaluationScheduleCommand(
            snapshot_id=snapshot_id,
            index_version="index-v1",
            model_version="model-v1",
            prompt_version="prompt-v1",
            threshold_version="threshold-v1",
            interval_seconds=3600,
        ),
        idempotency_key="schedule-key",
    )
    assert await service.enqueue_due(limit=10) == 1
    assert await service.enqueue_due(limit=10) == 0
    jobs = await service.list_jobs(_context("tenant-a"), limit=20, offset=0)
    assert len(jobs) == 1
    assert jobs[0].status.value == "pending"
    assert schedule.schedule_id
    assert await service.list_jobs(_context("tenant-b"), limit=20, offset=0) == ()
