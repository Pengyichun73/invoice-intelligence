"""PostgreSQL repository for isolated offline evaluation jobs."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_hex
from typing import Any, cast

from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError, WorkflowPersistenceError
from invoice_intelligence.application.ports.evaluation_jobs import EvaluationJobRepository
from invoice_intelligence.domain.evaluation import EvaluationRunStatus, EvaluationSuite
from invoice_intelligence.domain.evaluation_jobs import (
    DatasetSnapshot,
    EvaluationJob,
    EvaluationJobStatus,
    EvaluationSchedule,
    SnapshotCase,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import _run_from_payload
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    EvaluationDatasetRow,
    EvaluationJobReportRow,
    EvaluationJobRow,
    EvaluationRunRow,
    EvaluationScheduleRow,
    EvaluationSnapshotRow,
    MemoryBenefitJobCaseRow,
    MemoryBenefitRunRow,
)
from invoice_intelligence.infrastructure.reporting.postgres_evaluation import (
    report_artifacts_match,
)


class SQLAlchemyEvaluationJobRepository(EvaluationJobRepository):
    """Fact-source repository; all queue mutations are fenced by lease ownership."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def add_snapshot(self, snapshot: DatasetSnapshot) -> DatasetSnapshot:
        return await asyncio.to_thread(self._add_snapshot_sync, snapshot)

    async def get_snapshot(self, tenant_id: str, snapshot_id: str) -> DatasetSnapshot | None:
        return await asyncio.to_thread(self._get_snapshot_sync, tenant_id, snapshot_id)

    async def create_job(
        self, job: EvaluationJob, request_sha256: str, idempotency_key_hash: str | None,
    ) -> EvaluationJob:
        return await asyncio.to_thread(
            self._create_job_sync, job, request_sha256, idempotency_key_hash
        )

    async def create_memory_benefit_job(
        self, job: EvaluationJob, request_sha256: str,
        idempotency_key_hash: str, document_ids: tuple[str, ...],
    ) -> EvaluationJob:
        return await asyncio.to_thread(
            self._create_job_sync, job, request_sha256, idempotency_key_hash, document_ids
        )

    async def get_memory_benefit_documents(
        self, tenant_id: str, job_id: str
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(self._benefit_documents_sync, tenant_id, job_id)

    async def complete_memory_benefit(
        self, job: EvaluationJob, benefit_run_id: str
    ) -> None:
        await asyncio.to_thread(self._complete_memory_benefit_sync, job, benefit_run_id)

    async def get_job(self, tenant_id: str, job_id: str) -> EvaluationJob | None:
        return await asyncio.to_thread(self._get_job_sync, tenant_id, job_id)

    async def list_jobs(self, tenant_id: str, limit: int, offset: int) -> tuple[EvaluationJob, ...]:
        return await asyncio.to_thread(self._list_jobs_sync, tenant_id, limit, offset)

    async def claim(self, worker_id: str, lease_seconds: float, max_attempts: int) -> EvaluationJob | None:
        return await asyncio.to_thread(self._claim_sync, worker_id, lease_seconds, max_attempts)

    async def renew(self, job: EvaluationJob, lease_seconds: float) -> None:
        await asyncio.to_thread(self._renew_sync, job, lease_seconds)

    async def complete(self, job: EvaluationJob, report: dict[str, object]) -> None:
        await asyncio.to_thread(self._complete_sync, job, report)

    async def complete_suite(self, job: EvaluationJob, evaluation_run_id: str) -> None:
        await asyncio.to_thread(self._complete_suite_sync, job, evaluation_run_id)

    async def fail(self, job: EvaluationJob, code: str, retry: bool, max_attempts: int) -> None:
        await asyncio.to_thread(self._fail_sync, job, code, retry, max_attempts)

    async def recover_expired(self, max_attempts: int) -> int:
        return await asyncio.to_thread(self._recover_expired_sync, max_attempts)

    async def add_schedule(self, schedule: EvaluationSchedule) -> EvaluationSchedule:
        return await asyncio.to_thread(self._add_schedule_sync, schedule)

    async def get_schedule(self, tenant_id: str, schedule_id: str) -> EvaluationSchedule | None:
        return await asyncio.to_thread(self._get_schedule_sync, tenant_id, schedule_id)

    async def list_schedules(self, tenant_id: str) -> tuple[EvaluationSchedule, ...]:
        return await asyncio.to_thread(self._list_schedules_sync, tenant_id)

    async def disable_schedule(self, tenant_id: str, schedule_id: str) -> bool:
        return await asyncio.to_thread(self._disable_schedule_sync, tenant_id, schedule_id)

    async def enqueue_due(self, now: datetime, limit: int) -> int:
        return await asyncio.to_thread(self._enqueue_due_sync, now, limit)

    def _add_snapshot_sync(self, snapshot: DatasetSnapshot) -> DatasetSnapshot:
        try:
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(EvaluationSnapshotRow).where(
                        EvaluationSnapshotRow.tenant_id == snapshot.tenant_id,
                        EvaluationSnapshotRow.dataset_version == snapshot.dataset_version,
                    )
                )
                payload = [_case_payload(case) for case in snapshot.cases]
                if existing is not None:
                    if existing.content_sha256 != snapshot.content_sha256:
                        raise ResourceConflictError("Dataset snapshot version is immutable")
                    return _snapshot_from_row(existing)
                session.add(
                    EvaluationSnapshotRow(
                        snapshot_id=snapshot.snapshot_id,
                        tenant_id=snapshot.tenant_id,
                        dataset_version=snapshot.dataset_version,
                        schema_version=snapshot.schema_version,
                        content_sha256=snapshot.content_sha256,
                        cases_json=payload,
                        created_at=snapshot.created_at,
                    )
                )
                return snapshot
        except ResourceConflictError:
            raise
        except IntegrityError as exc:
            raise ResourceConflictError("Dataset snapshot already exists") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist evaluation snapshot") from exc

    def _get_snapshot_sync(self, tenant_id: str, snapshot_id: str) -> DatasetSnapshot | None:
        with self._sessions() as session:
            row = session.scalar(
                select(EvaluationSnapshotRow).where(
                    EvaluationSnapshotRow.tenant_id == tenant_id,
                    EvaluationSnapshotRow.snapshot_id == snapshot_id,
                )
            )
            return _snapshot_from_row(row) if row is not None else None

    def _create_job_sync(
        self, job: EvaluationJob, request_sha256: str, idempotency_key_hash: str | None,
        document_ids: tuple[str, ...] = (),
    ) -> EvaluationJob:
        try:
            with self._sessions.begin() as session:
                if idempotency_key_hash is not None:
                    existing = session.scalar(
                        select(EvaluationJobRow).where(
                            EvaluationJobRow.tenant_id == job.tenant_id,
                            EvaluationJobRow.idempotency_key_hash == idempotency_key_hash,
                        )
                    )
                    if existing is not None:
                        if (
                            existing.request_sha256 != request_sha256
                            or existing.evidence_class != job.evidence_class
                        ):
                            raise ResourceConflictError("Idempotency key was reused with different content")
                        return _job_from_row(existing)
                dataset_key = None
                if job.evidence_class == "suite_run":
                    dataset = session.scalar(select(EvaluationDatasetRow).where(
                        EvaluationDatasetRow.tenant_id == job.tenant_id,
                        EvaluationDatasetRow.dataset_id == job.dataset_id,
                        EvaluationDatasetRow.dataset_version == job.dataset_version,
                        EvaluationDatasetRow.schema_version == job.schema_version,
                    ))
                    if dataset is None:
                        raise ResourceConflictError("Evaluation dataset binding is unavailable")
                    dataset_key = dataset.dataset_key
                session.add(
                    EvaluationJobRow(
                        job_id=job.job_id,
                        tenant_id=job.tenant_id,
                        snapshot_id=job.snapshot_id,
                        dataset_key=dataset_key,
                        dataset_id=job.dataset_id,
                        evidence_class=job.evidence_class,
                        suite=job.suite.value if job.suite is not None else None,
                        retrieval_policy_version=job.retrieval_policy_version,
                        catalog_version=job.catalog_version,
                        admission_policy_version=job.admission_policy_version,
                        field_binding_policy_version=job.field_binding_policy_version,
                        evaluation_run_id=None,
                        dataset_version=job.dataset_version,
                        schema_version=job.schema_version,
                        index_version=job.index_version,
                        model_version=job.model_version,
                        prompt_version=job.prompt_version,
                        threshold_version=job.threshold_version,
                        request_sha256=request_sha256,
                        idempotency_key_hash=idempotency_key_hash,
                        status=job.status.value,
                        attempt_count=job.attempt_count,
                        next_attempt_at=job.next_attempt_at,
                        created_at=job.created_at,
                        updated_at=job.updated_at,
                    )
                )
                if job.evidence_class == "memory_benefit":
                    if not document_ids or len(document_ids) != len(set(document_ids)):
                        raise ResourceConflictError("Memory benefit Job case list is invalid")
                    session.flush()
                    for ordinal, document_id in enumerate(document_ids):
                        session.add(MemoryBenefitJobCaseRow(
                            job_id=job.job_id, ordinal=ordinal,
                            tenant_id=job.tenant_id, document_id=document_id,
                        ))
                return job
        except ResourceConflictError:
            raise
        except IntegrityError as exc:
            raise ResourceConflictError("Evaluation job already exists") from exc
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to persist evaluation job") from exc

    def _benefit_documents_sync(self, tenant_id: str, job_id: str) -> tuple[str, ...]:
        with self._sessions() as session:
            job = session.scalar(select(EvaluationJobRow).where(
                EvaluationJobRow.tenant_id == tenant_id,
                EvaluationJobRow.job_id == job_id,
                EvaluationJobRow.evidence_class == "memory_benefit",
            ))
            if job is None:
                return ()
            return tuple(session.scalars(select(MemoryBenefitJobCaseRow.document_id).where(
                MemoryBenefitJobCaseRow.tenant_id == tenant_id,
                MemoryBenefitJobCaseRow.job_id == job_id,
            ).order_by(MemoryBenefitJobCaseRow.ordinal)))

    def _get_job_sync(self, tenant_id: str, job_id: str) -> EvaluationJob | None:
        with self._sessions() as session:
            row = session.scalar(
                select(EvaluationJobRow).where(
                    EvaluationJobRow.tenant_id == tenant_id,
                    EvaluationJobRow.job_id == job_id,
                )
            )
            if row is None:
                return None
            job = _job_from_row(row)
            report = session.scalar(
                select(EvaluationJobReportRow).where(
                    EvaluationJobReportRow.tenant_id == tenant_id,
                    EvaluationJobReportRow.job_id == job_id,
                )
            )
            return replace(job, report=report.metrics_json) if report is not None else job

    def _list_jobs_sync(self, tenant_id: str, limit: int, offset: int) -> tuple[EvaluationJob, ...]:
        if limit < 1 or limit > 200 or offset < 0:
            raise ValueError("Invalid evaluation job pagination")
        with self._sessions() as session:
            rows = session.scalars(
                select(EvaluationJobRow)
                .where(EvaluationJobRow.tenant_id == tenant_id)
                .order_by(EvaluationJobRow.created_at.desc(), EvaluationJobRow.job_id.desc())
                .offset(offset).limit(limit)
            ).all()
            return tuple(_job_from_row(row) for row in rows)

    def _claim_sync(self, worker_id: str, lease_seconds: float, max_attempts: int) -> EvaluationJob | None:
        now = datetime.now(UTC)
        try:
            with self._sessions.begin() as session:
                row = session.scalar(
                    select(EvaluationJobRow)
                    .where(
                        (
                            (EvaluationJobRow.status == EvaluationJobStatus.PENDING.value)
                            | (
                                (EvaluationJobRow.status == EvaluationJobStatus.FAILED.value)
                                & (EvaluationJobRow.next_attempt_at <= now)
                            )
                            | (
                                (EvaluationJobRow.status == EvaluationJobStatus.RUNNING.value)
                                & (EvaluationJobRow.lease_expires_at < now)
                            )
                        ),
                        EvaluationJobRow.attempt_count < max_attempts,
                    )
                    .order_by(EvaluationJobRow.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if row is None:
                    return None
                row.status = EvaluationJobStatus.RUNNING.value
                row.attempt_count += 1
                row.worker_id = worker_id
                row.lease_token = token_hex(24)
                row.lease_expires_at = now + timedelta(seconds=lease_seconds)
                row.next_attempt_at = None
                row.updated_at = now
                return _job_from_row(row)
        except SQLAlchemyError as exc:
            raise WorkflowPersistenceError("Unable to claim evaluation job") from exc

    def _assert_lease(self, session: Session, job: EvaluationJob) -> EvaluationJobRow:
        row = session.scalar(
            select(EvaluationJobRow).where(
                EvaluationJobRow.job_id == job.job_id,
                EvaluationJobRow.worker_id == job.worker_id,
                EvaluationJobRow.lease_token == job.lease_token,
                EvaluationJobRow.status == EvaluationJobStatus.RUNNING.value,
                EvaluationJobRow.lease_expires_at > datetime.now(UTC),
            ).with_for_update()
        )
        if row is None:
            raise ResourceConflictError("Evaluation job lease is no longer valid")
        return row

    def _renew_sync(self, job: EvaluationJob, lease_seconds: float) -> None:
        with self._sessions.begin() as session:
            row = self._assert_lease(session, job)
            row.lease_expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
            row.updated_at = datetime.now(UTC)

    def _complete_sync(self, job: EvaluationJob, report: dict[str, object]) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._assert_lease(session, job)
            if row.evidence_class != "diagnostic_only":
                raise ResourceConflictError("Suite Job cannot accept diagnostic report")
            row.status = EvaluationJobStatus.COMPLETED.value
            row.worker_id = row.lease_token = row.lease_expires_at = None
            row.updated_at = now
            session.add(
                EvaluationJobReportRow(
                    report_id=sha256(f"report:{job.job_id}".encode()).hexdigest(),
                    job_id=job.job_id,
                    tenant_id=row.tenant_id,
                    metrics_json=report,
                    created_at=now,
                )
            )

    def _complete_suite_sync(self, job: EvaluationJob, evaluation_run_id: str) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._assert_lease(session, job)
            if row.evidence_class != "suite_run":
                raise ResourceConflictError("Diagnostic Job cannot bind a Suite Run")
            run_row = session.scalar(select(EvaluationRunRow).where(
                EvaluationRunRow.tenant_id == row.tenant_id,
                EvaluationRunRow.evaluation_run_id == evaluation_run_id,
            ))
            if run_row is None or run_row.status != EvaluationRunStatus.COMPLETED.value:
                raise ResourceConflictError("Completed Suite Run is unavailable")
            try:
                run = _run_from_payload(run_row.run_json)
            except (KeyError, TypeError, ValueError) as exc:
                raise ResourceConflictError("Suite Run evidence is invalid") from exc
            if (
                run.status is not EvaluationRunStatus.COMPLETED
                or run.evaluation_run_id != evaluation_run_id
                or run.tenant_id != row.tenant_id
                or run_row.dataset_key != row.dataset_key
                or run.dataset_id != row.dataset_id
                or run.dataset_version != row.dataset_version
                or run.schema_version != row.schema_version
                or run.suite.value != row.suite
                or run.bindings.index_version.value != row.index_version
                or run.bindings.model_version.value != row.model_version
                or run.bindings.prompt_version.value != row.prompt_version
                or run.bindings.retrieval_policy_version.value != row.retrieval_policy_version
                or run.bindings.threshold_version != row.threshold_version
                or run.bindings.catalog_version != row.catalog_version
                or run.bindings.admission_policy_version != row.admission_policy_version
                or run.bindings.field_binding_policy_version != row.field_binding_policy_version
                or run.report_schema_version != "invoice-offline-evaluation-v2"
                or not run.artifact_references
                or not run.leakage_check_passed
                or not report_artifacts_match(session, run_row, run)
            ):
                raise ResourceConflictError("Suite Job and Run bindings do not match")
            row.status = EvaluationJobStatus.COMPLETED.value
            row.evaluation_run_id = evaluation_run_id
            row.worker_id = row.lease_token = row.lease_expires_at = None
            row.updated_at = now
            session.add(EvaluationJobReportRow(
                report_id=sha256(f"report:{job.job_id}".encode()).hexdigest(),
                job_id=job.job_id,
                tenant_id=row.tenant_id,
                metrics_json={
                    "evidence_class": "suite_run",
                    "evaluation_run_id": evaluation_run_id,
                    "suite": run.suite.value,
                    "report_schema_version": run.report_schema_version,
                    "variant_count": len(run.results),
                    "artifact_count": len(run.artifact_references),
                },
                created_at=now,
            ))

    def _complete_memory_benefit_sync(
        self, job: EvaluationJob, benefit_run_id: str
    ) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._assert_lease(session, job)
            if row.evidence_class != "memory_benefit":
                raise ResourceConflictError("Job is not a memory benefit evaluation")
            benefit = session.scalar(select(MemoryBenefitRunRow).where(
                MemoryBenefitRunRow.tenant_id == row.tenant_id,
                MemoryBenefitRunRow.run_id == benefit_run_id,
                MemoryBenefitRunRow.status == "completed",
            ))
            document_count = len(session.scalars(select(MemoryBenefitJobCaseRow.document_id).where(
                MemoryBenefitJobCaseRow.tenant_id == row.tenant_id,
                MemoryBenefitJobCaseRow.job_id == row.job_id,
            )).all())
            if benefit is None or (
                row.dataset_version != benefit.dataset_digest
                or row.schema_version != benefit.schema_version
                or row.catalog_version != benefit.catalog_version
                or row.index_version != benefit.index_version
                or row.model_version != benefit.model_version
                or row.prompt_version != benefit.prompt_version
                or benefit.case_count != document_count
            ):
                raise ResourceConflictError("Benefit Run and Job bindings do not match")
            row.status = EvaluationJobStatus.COMPLETED.value
            row.evaluation_run_id = benefit_run_id
            row.worker_id = row.lease_token = row.lease_expires_at = None
            row.updated_at = now
            session.add(EvaluationJobReportRow(
                report_id=sha256(f"report:{job.job_id}".encode()).hexdigest(),
                job_id=job.job_id, tenant_id=row.tenant_id,
                metrics_json={
                    "evidence_class": "memory_benefit",
                    "benefit_run_id": benefit_run_id,
                    "case_count": benefit.case_count,
                    "coverage_sufficient": benefit.metrics_json.get("coverage_sufficient") is True,
                    "passed": benefit.metrics_json.get("passed") is True,
                },
                created_at=now,
            ))

    def _fail_sync(self, job: EvaluationJob, code: str, retry: bool, max_attempts: int) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._assert_lease(session, job)
            should_retry = retry and row.attempt_count < max_attempts
            row.status = EvaluationJobStatus.FAILED.value if should_retry else EvaluationJobStatus.QUARANTINED.value
            row.failure_code = code[:128]
            row.next_attempt_at = now + timedelta(seconds=min(300, 2 ** row.attempt_count)) if should_retry else None
            row.worker_id = row.lease_token = row.lease_expires_at = None
            row.updated_at = now

    def _recover_expired_sync(self, max_attempts: int) -> int:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            rows = session.scalars(
                select(EvaluationJobRow).where(
                    EvaluationJobRow.status == EvaluationJobStatus.RUNNING.value,
                    EvaluationJobRow.lease_expires_at < now,
                ).with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.status = EvaluationJobStatus.FAILED.value if row.attempt_count < max_attempts else EvaluationJobStatus.QUARANTINED.value
                row.failure_code = "lease_expired"
                row.next_attempt_at = now if row.attempt_count < max_attempts else None
                row.worker_id = row.lease_token = row.lease_expires_at = None
                row.updated_at = now
            return len(rows)

    def _add_schedule_sync(self, schedule: EvaluationSchedule) -> EvaluationSchedule:
        with self._sessions.begin() as session:
            session.add(EvaluationScheduleRow(**_schedule_payload(schedule)))
        return schedule

    def _get_schedule_sync(self, tenant_id: str, schedule_id: str) -> EvaluationSchedule | None:
        with self._sessions() as session:
            row = session.scalar(select(EvaluationScheduleRow).where(
                EvaluationScheduleRow.tenant_id == tenant_id,
                EvaluationScheduleRow.schedule_id == schedule_id,
            ))
            return _schedule_from_row(row) if row is not None else None

    def _list_schedules_sync(self, tenant_id: str) -> tuple[EvaluationSchedule, ...]:
        with self._sessions() as session:
            rows = session.scalars(select(EvaluationScheduleRow).where(
                EvaluationScheduleRow.tenant_id == tenant_id
            ).order_by(EvaluationScheduleRow.schedule_id)).all()
            return tuple(_schedule_from_row(row) for row in rows)

    def _disable_schedule_sync(self, tenant_id: str, schedule_id: str) -> bool:
        with self._sessions.begin() as session:
            row = session.scalar(select(EvaluationScheduleRow).where(
                EvaluationScheduleRow.tenant_id == tenant_id,
                EvaluationScheduleRow.schedule_id == schedule_id,
            ).with_for_update())
            if row is None:
                return False
            row.enabled = False
            return True

    def _enqueue_due_sync(self, now: datetime, limit: int) -> int:
        with self._sessions.begin() as session:
            rows = session.scalars(select(EvaluationScheduleRow).where(
                EvaluationScheduleRow.enabled.is_(True),
                EvaluationScheduleRow.next_run_at <= now,
            ).order_by(EvaluationScheduleRow.next_run_at).limit(limit).with_for_update(skip_locked=True)).all()
            for row in rows:
                snapshot = session.scalar(select(EvaluationSnapshotRow).where(
                    EvaluationSnapshotRow.tenant_id == row.tenant_id,
                    EvaluationSnapshotRow.snapshot_id == row.snapshot_id,
                ))
                if snapshot is None:
                    row.enabled = False
                    continue
                scheduled_at = row.next_run_at
                job_id = sha256(
                    f"schedule:{row.schedule_id}:{scheduled_at.isoformat()}".encode()
                ).hexdigest()
                if session.get(EvaluationJobRow, job_id) is None:
                    session.add(EvaluationJobRow(
                        job_id=job_id, tenant_id=row.tenant_id,
                        snapshot_id=row.snapshot_id,
                        dataset_key=None,
                        dataset_id=None,
                        evidence_class="diagnostic_only",
                        dataset_version=snapshot.dataset_version,
                        schema_version=snapshot.schema_version,
                        index_version=row.index_version,
                        model_version=row.model_version,
                        prompt_version=row.prompt_version,
                        threshold_version=row.threshold_version,
                        request_sha256=sha256(job_id.encode()).hexdigest(),
                        idempotency_key_hash=None,
                        status=EvaluationJobStatus.PENDING.value,
                        attempt_count=0, next_attempt_at=None,
                        lease_expires_at=None, worker_id=None,
                        lease_token=None, failure_code=None,
                        created_at=now, updated_at=now,
                    ))
                row.next_run_at = now + timedelta(seconds=row.interval_seconds)
            return len(rows)


def _case_payload(case: SnapshotCase) -> dict[str, object]:
    return {
        "case_id": case.case_id, "field_path": case.field_path,
        "expected_present": case.expected_present, "predicted_present": case.predicted_present,
        "field_correct": case.field_correct, "evidence_covered": case.evidence_covered,
        "amount_absolute_error": case.amount_absolute_error, "review_required": case.review_required,
        "negative_false_recall": case.negative_false_recall,
        "ocr_vision_consistent": case.ocr_vision_consistent,
    }


def _snapshot_from_row(row: EvaluationSnapshotRow) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id=row.snapshot_id, tenant_id=row.tenant_id,
        dataset_version=row.dataset_version, schema_version=row.schema_version,
        cases=tuple(SnapshotCase(**cast(dict[str, Any], item)) for item in row.cases_json),
        content_sha256=row.content_sha256, created_at=_aware(row.created_at),
    )


def _job_from_row(row: EvaluationJobRow) -> EvaluationJob:
    return EvaluationJob(
        job_id=row.job_id, tenant_id=row.tenant_id, snapshot_id=row.snapshot_id,
        dataset_version=row.dataset_version, schema_version=row.schema_version,
        index_version=row.index_version, model_version=row.model_version,
        prompt_version=row.prompt_version, threshold_version=row.threshold_version,
        status=EvaluationJobStatus(row.status), attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at, lease_expires_at=row.lease_expires_at,
        worker_id=row.worker_id, lease_token=row.lease_token, failure_code=row.failure_code,
        report=None, created_at=_aware(row.created_at), updated_at=_aware(row.updated_at),
        evidence_class=row.evidence_class,
        dataset_id=row.dataset_id,
        suite=EvaluationSuite(row.suite) if row.suite is not None else None,
        retrieval_policy_version=row.retrieval_policy_version,
        catalog_version=row.catalog_version,
        admission_policy_version=row.admission_policy_version,
        field_binding_policy_version=row.field_binding_policy_version,
        evaluation_run_id=row.evaluation_run_id,
    )


def _schedule_payload(schedule: EvaluationSchedule) -> dict[str, Any]:
    return {
        "schedule_id": schedule.schedule_id, "tenant_id": schedule.tenant_id,
        "snapshot_id": schedule.snapshot_id, "index_version": schedule.index_version,
        "model_version": schedule.model_version, "prompt_version": schedule.prompt_version,
        "threshold_version": schedule.threshold_version, "interval_seconds": schedule.interval_seconds,
        "next_run_at": schedule.next_run_at, "enabled": schedule.enabled,
    }


def _schedule_from_row(row: EvaluationScheduleRow) -> EvaluationSchedule:
    return EvaluationSchedule(
        schedule_id=row.schedule_id, tenant_id=row.tenant_id, snapshot_id=row.snapshot_id,
        index_version=row.index_version, model_version=row.model_version,
        prompt_version=row.prompt_version, threshold_version=row.threshold_version,
        interval_seconds=row.interval_seconds, next_run_at=_aware(row.next_run_at), enabled=row.enabled,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
