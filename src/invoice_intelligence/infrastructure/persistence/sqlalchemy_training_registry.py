"""PostgreSQL-backed Training Job, export, artifact, and audit registries."""

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    WorkflowPersistenceError,
)
from invoice_intelligence.domain.training import (
    ModelArtifact,
    ModelLifecycleStage,
    TrainingRunStatus,
)
from invoice_intelligence.domain.training_registry import (
    TERMINAL_TRAINING_JOB_STATUSES,
    TrainingArtifact,
    TrainingDatasetExport,
    TrainingJob,
    TrainingJobLease,
    TrainingJobStatus,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_model_training import (
    _artifact_payload as _model_artifact_payload,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_model_training import (
    _training_run_from_payload,
    _training_run_payload,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    ModelArtifactRow,
    ModelVersionRow,
    TrainingArtifactRow,
    TrainingAuditEventRow,
    TrainingDatasetExportRow,
    TrainingDatasetRecordRow,
    TrainingDatasetVersionRow,
    TrainingJobRow,
    TrainingRunRow,
)


class SQLAlchemyTrainingRegistryRepository:
    """Apply tenant-scoped CAS transitions and worker leases transactionally."""

    def __init__(self, engine: Engine) -> None:
        self._dialect = engine.dialect.name
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def create_job(
        self,
        job: TrainingJob,
        *,
        idempotency_digest: str,
        dataset_export: TrainingDatasetExport,
    ) -> TrainingJob:
        return await asyncio.to_thread(
            self._create_job_sync, job, idempotency_digest, dataset_export
        )

    async def get_job(self, tenant_id: str, job_id: str) -> TrainingJob | None:
        return await asyncio.to_thread(self._get_job_sync, tenant_id, job_id)

    async def get_dataset_export(self, export_id: str) -> TrainingDatasetExport | None:
        return await asyncio.to_thread(self._get_export_sync, export_id)

    async def request_cancel(
        self,
        tenant_id: str,
        job_id: str,
        *,
        actor_id: str,
        reason: str,
        idempotency_digest: str,
    ) -> TrainingJob:
        return await asyncio.to_thread(
            self._request_cancel_sync,
            tenant_id,
            job_id,
            actor_id,
            reason,
            idempotency_digest,
        )

    async def create_retry(
        self,
        source: TrainingJob,
        retry: TrainingJob,
        *,
        idempotency_digest: str,
    ) -> TrainingJob:
        dataset_export = await self.get_dataset_export(source.dataset_export_id)
        if dataset_export is None:
            raise ResourceConflictError("Training dataset export is unavailable")
        return await self.create_job(
            retry,
            idempotency_digest=idempotency_digest,
            dataset_export=dataset_export,
        )

    async def claim_jobs(
        self, *, worker_id: str, limit: int, lease_seconds: float
    ) -> tuple[TrainingJobLease, ...]:
        return await asyncio.to_thread(
            self._claim_jobs_sync, worker_id, limit, lease_seconds
        )

    async def advance(
        self,
        lease: TrainingJobLease,
        *,
        status: TrainingJobStatus,
        remote_job_id: str | None = None,
        failure_code: str | None = None,
    ) -> TrainingJob:
        return await asyncio.to_thread(
            self._advance_sync, lease, status, remote_job_id, failure_code
        )

    async def reschedule(
        self,
        lease: TrainingJobLease,
        *,
        error_code: str,
        delay_seconds: float,
        max_attempts: int,
    ) -> TrainingJob:
        return await asyncio.to_thread(
            self._reschedule_sync,
            lease,
            error_code,
            delay_seconds,
            max_attempts,
        )

    async def release(self, lease: TrainingJobLease) -> None:
        await asyncio.to_thread(self._release_sync, lease)

    async def commit_success(
        self, lease: TrainingJobLease, artifact: TrainingArtifact
    ) -> TrainingJob:
        return await asyncio.to_thread(self._commit_success_sync, lease, artifact)

    def _create_job_sync(
        self,
        job: TrainingJob,
        idempotency_digest: str,
        dataset_export: TrainingDatasetExport,
    ) -> TrainingJob:
        try:
            with self._sessions.begin() as session:
                replay = session.scalar(
                    select(TrainingJobRow).where(
                        TrainingJobRow.tenant_id == job.tenant_id,
                        TrainingJobRow.idempotency_digest == idempotency_digest,
                    )
                )
                if replay is not None:
                    existing = _job_from_row(replay)
                    if _immutable_job(existing) != _immutable_job(job):
                        raise ResourceConflictError(
                            "Idempotency key was reused with different training bindings"
                        )
                    return existing
                dataset_key = _dataset_key(
                    dataset_export.tenant_scope,
                    dataset_export.dataset_id,
                    dataset_export.dataset_version,
                )
                dataset_row = session.get(TrainingDatasetVersionRow, dataset_key)
                if dataset_row is None or dataset_row.status != "exported":
                    raise ResourceConflictError("Training dataset is not exported")
                if (
                    job.tenant_id not in dataset_row.source_tenant_ids_json
                    or dataset_row.schema_version != job.schema_version
                ):
                    raise ResourceConflictError(
                        "Training dataset does not match tenant and Schema"
                    )
                records = tuple(
                    session.scalars(
                        select(TrainingDatasetRecordRow).where(
                            TrainingDatasetRecordRow.dataset_key == dataset_key
                        )
                    )
                )
                hard_negative_count = sum(
                    len(record.record_json["record"]["hard_negatives"])
                    for record in records
                )
                if (
                    dataset_export.record_count != len(records)
                    or dataset_export.positive_count != len(records)
                    or dataset_export.hard_negative_count != hard_negative_count
                ):
                    raise ResourceConflictError(
                        "Dataset export counts do not match immutable records"
                    )
                export_row = session.get(TrainingDatasetExportRow, dataset_export.export_id)
                if export_row is None:
                    session.add(_export_row(dataset_export, dataset_key))
                elif export_row.export_json != _export_payload(dataset_export):
                    raise ResourceConflictError("Dataset export identity is immutable")
                run_row = session.scalar(
                    select(TrainingRunRow).where(
                        TrainingRunRow.tenant_id == job.tenant_id,
                        TrainingRunRow.training_run_id == job.training_run_id,
                    )
                )
                row = _job_row(job, idempotency_digest, run_row)
                session.add(row)
                self._audit(session, job, None, "created", job.requested_by, "created")
            return job
        except (ResourceConflictError,):
            raise
        except (IntegrityError, SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowPersistenceError("Training job persistence failed") from exc

    def _get_job_sync(self, tenant_id: str, job_id: str) -> TrainingJob | None:
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(TrainingJobRow).where(
                        TrainingJobRow.tenant_id == tenant_id,
                        TrainingJobRow.job_id == job_id,
                    )
                )
                return _job_from_row(row) if row is not None else None
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowPersistenceError("Training job read failed") from exc

    def _get_export_sync(self, export_id: str) -> TrainingDatasetExport | None:
        try:
            with self._sessions() as session:
                row = session.get(TrainingDatasetExportRow, export_id)
                return _export_from_payload(row.export_json) if row is not None else None
        except (SQLAlchemyError, KeyError, TypeError, ValueError) as exc:
            raise WorkflowPersistenceError("Training dataset export read failed") from exc

    def _request_cancel_sync(
        self,
        tenant_id: str,
        job_id: str,
        actor_id: str,
        reason: str,
        idempotency_digest: str,
    ) -> TrainingJob:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._locked_row(session, tenant_id, job_id)
            current = _job_from_row(row)
            if current.status in TERMINAL_TRAINING_JOB_STATUSES:
                return current
            if current.cancel_requested_at is not None:
                if current.cancel_requested_by != actor_id or current.cancel_reason != reason:
                    raise ResourceConflictError("Cancellation was already requested")
                return current
            updated = replace(
                current,
                cancel_requested_at=now,
                cancel_requested_by=actor_id,
                cancel_reason=reason,
                next_attempt_at=now,
                updated_at=now,
                revision=current.revision + 1,
            )
            self._write_job(row, updated)
            self._audit(
                session,
                updated,
                current.status,
                "cancel_requested",
                actor_id,
                idempotency_digest,
            )
            return updated

    def _claim_jobs_sync(
        self, worker_id: str, limit: int, lease_seconds: float
    ) -> tuple[TrainingJobLease, ...]:
        now = datetime.now(UTC)
        query = (
            select(TrainingJobRow)
            .where(
                TrainingJobRow.status.in_(("planned", "submitted", "running")),
                or_(
                    TrainingJobRow.next_attempt_at.is_(None),
                    TrainingJobRow.next_attempt_at <= now,
                ),
                or_(
                    TrainingJobRow.lease_expires_at.is_(None),
                    TrainingJobRow.lease_expires_at <= now,
                ),
            )
            .order_by(TrainingJobRow.created_at, TrainingJobRow.job_id)
            .limit(limit)
        )
        if self._dialect == "postgresql":
            query = query.with_for_update(skip_locked=True)
        with self._sessions.begin() as session:
            rows = tuple(session.scalars(query))
            leases: list[TrainingJobLease] = []
            for row in rows:
                current = _job_from_row(row)
                token = uuid4().hex + uuid4().hex
                updated = replace(
                    current,
                    worker_id=worker_id,
                    lease_token=token,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    claim_count=current.claim_count + 1,
                    revision=current.revision + 1,
                    updated_at=now,
                )
                self._write_job(row, updated)
                self._audit(session, updated, current.status, "claimed", worker_id, "worker_claim")
                leases.append(TrainingJobLease(updated, worker_id, token, updated.revision))
            return tuple(leases)

    def _advance_sync(
        self,
        lease: TrainingJobLease,
        status: TrainingJobStatus,
        remote_job_id: str | None,
        failure_code: str | None,
    ) -> TrainingJob:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row, current = self._lease_row(session, lease)
            if not current.can_transition_to(status):
                raise ResourceConflictError("Training job status transition is invalid")
            completed_at = now if status in TERMINAL_TRAINING_JOB_STATUSES else None
            updated = replace(
                current,
                status=status,
                remote_job_id=remote_job_id or current.remote_job_id,
                submitted_at=(
                    now
                    if status is TrainingJobStatus.SUBMITTED
                    else current.submitted_at
                ),
                started_at=(now if status is TrainingJobStatus.RUNNING else current.started_at),
                completed_at=completed_at,
                failure_code=failure_code,
                failure_attempt_count=0,
                next_attempt_at=None,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                revision=current.revision + 1,
            )
            self._write_job(row, updated)
            self._sync_run(session, updated)
            self._audit(
                session,
                updated,
                current.status,
                "status_changed",
                lease.worker_id,
                failure_code or "ok",
            )
            return updated

    def _reschedule_sync(
        self,
        lease: TrainingJobLease,
        error_code: str,
        delay_seconds: float,
        max_attempts: int,
    ) -> TrainingJob:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row, current = self._lease_row(session, lease)
            attempts = current.failure_attempt_count + 1
            terminal = attempts >= max_attempts
            updated = replace(
                current,
                status=(TrainingJobStatus.QUARANTINED if terminal else current.status),
                failure_attempt_count=attempts,
                next_attempt_at=(None if terminal else now + timedelta(seconds=delay_seconds)),
                failure_code=(error_code if terminal else None),
                completed_at=(now if terminal else None),
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                revision=current.revision + 1,
            )
            self._write_job(row, updated)
            if terminal:
                self._sync_run(session, updated)
            self._audit(
                session,
                updated,
                current.status,
                "retry_scheduled",
                lease.worker_id,
                error_code,
            )
            return updated

    def _release_sync(self, lease: TrainingJobLease) -> None:
        with self._sessions.begin() as session:
            row, current = self._lease_row(session, lease)
            updated = replace(
                current,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=datetime.now(UTC),
                revision=current.revision + 1,
            )
            self._write_job(row, updated)
            self._audit(session, updated, current.status, "released", lease.worker_id, "released")

    def _commit_success_sync(
        self, lease: TrainingJobLease, artifact: TrainingArtifact
    ) -> TrainingJob:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row, current = self._lease_row(session, lease)
            if artifact.job_id != current.job_id or artifact.tenant_id != current.tenant_id:
                raise ResourceConflictError("Training artifact crosses a job boundary")
            if current.status not in {TrainingJobStatus.SUBMITTED, TrainingJobStatus.RUNNING}:
                raise ResourceConflictError("Only submitted training can succeed")
            if session.get(TrainingArtifactRow, artifact.artifact_id) is None:
                session.add(
                    TrainingArtifactRow(
                        artifact_id=artifact.artifact_id,
                        tenant_id=artifact.tenant_id,
                        job_id=artifact.job_id,
                        training_run_id=artifact.training_run_id,
                        remote_job_id=artifact.remote_job_id,
                        provider=artifact.provider,
                        source_uri=artifact.source_uri,
                        checksum_sha256=artifact.checksum_sha256,
                        size_bytes=artifact.size_bytes,
                        artifact_json=_artifact_payload(artifact),
                        created_at=artifact.created_at,
                    )
                )
            updated = replace(
                current,
                status=TrainingJobStatus.SUCCEEDED,
                completed_at=now,
                failure_attempt_count=0,
                next_attempt_at=None,
                worker_id=None,
                lease_token=None,
                lease_expires_at=None,
                updated_at=now,
                revision=current.revision + 1,
            )
            self._write_job(row, updated)
            self._sync_run(session, updated, artifact.source_uri)
            self._register_model_artifact(session, updated, artifact)
            self._audit(
                session,
                updated,
                current.status,
                "artifact_registered",
                lease.worker_id,
                "verified",
            )
            return updated

    def _locked_row(self, session: Session, tenant_id: str, job_id: str) -> TrainingJobRow:
        row = session.scalar(
            select(TrainingJobRow)
            .where(TrainingJobRow.tenant_id == tenant_id, TrainingJobRow.job_id == job_id)
            .with_for_update()
        )
        if row is None:
            raise ResourceConflictError("Training job was not found")
        return row

    def _lease_row(
        self, session: Session, lease: TrainingJobLease
    ) -> tuple[TrainingJobRow, TrainingJob]:
        row = session.scalar(
            select(TrainingJobRow)
            .where(
                TrainingJobRow.job_id == lease.job.job_id,
                TrainingJobRow.worker_id == lease.worker_id,
                TrainingJobRow.lease_token == lease.lease_token,
                TrainingJobRow.revision == lease.revision,
                TrainingJobRow.lease_expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
        if row is None:
            raise ResourceConflictError("Training worker lease is stale")
        return row, _job_from_row(row)

    @staticmethod
    def _write_job(row: TrainingJobRow, job: TrainingJob) -> None:
        row.status = job.status.value
        row.remote_job_id = job.remote_job_id
        row.revision = job.revision
        row.claim_count = job.claim_count
        row.failure_attempt_count = job.failure_attempt_count
        row.next_attempt_at = job.next_attempt_at
        row.worker_id = job.worker_id
        row.lease_token = job.lease_token
        row.lease_expires_at = job.lease_expires_at
        row.cancel_requested_at = job.cancel_requested_at
        row.cancel_requested_by = job.cancel_requested_by
        row.cancel_reason = job.cancel_reason
        row.job_json = _job_payload(job)
        row.updated_at = job.updated_at
        row.completed_at = job.completed_at

    @staticmethod
    def _audit(
        session: Session,
        job: TrainingJob,
        from_status: TrainingJobStatus | None,
        action: str,
        actor_id: str,
        reason_code: str,
    ) -> None:
        session.add(
            TrainingAuditEventRow(
                event_id=sha256(f"{job.job_id}\0{job.revision}".encode()).hexdigest(),
                tenant_id=job.tenant_id,
                job_id=job.job_id,
                action=action,
                actor_id=actor_id[:128],
                from_status=from_status.value if from_status else None,
                to_status=job.status.value,
                revision=job.revision,
                reason_code=_safe_code(reason_code),
                trace_id=job.trace_id,
                created_at=job.updated_at,
            )
        )

    @staticmethod
    def _sync_run(
        session: Session, job: TrainingJob, artifact_reference: str | None = None
    ) -> None:
        row = session.scalar(
            select(TrainingRunRow).where(
                TrainingRunRow.tenant_id == job.tenant_id,
                TrainingRunRow.training_run_id == job.training_run_id,
            )
        )
        if row is None:
            return
        run = _training_run_from_payload(row.run_json)
        mapping = {
            TrainingJobStatus.PLANNED: TrainingRunStatus.EXPORT_READY,
            TrainingJobStatus.SUBMITTED: TrainingRunStatus.SUBMITTED,
            TrainingJobStatus.RUNNING: TrainingRunStatus.RUNNING,
            TrainingJobStatus.SUCCEEDED: TrainingRunStatus.SUCCEEDED,
            TrainingJobStatus.FAILED: TrainingRunStatus.FAILED,
            TrainingJobStatus.CANCELLED: TrainingRunStatus.CANCELED,
            TrainingJobStatus.QUARANTINED: TrainingRunStatus.FAILED,
        }
        target = mapping[job.status]
        if job.remote_job_id is None and job.status in {
            TrainingJobStatus.FAILED,
            TrainingJobStatus.CANCELLED,
            TrainingJobStatus.QUARANTINED,
        }:
            target = TrainingRunStatus.UNSUPPORTED
        run_failure_code: str | None = None
        if target is TrainingRunStatus.FAILED:
            run_failure_code = job.failure_code
        elif target is TrainingRunStatus.UNSUPPORTED:
            run_failure_code = job.failure_code or "TRAINING_TERMINATED_BEFORE_SUBMISSION"
        updated = replace(
            run,
            status=target,
            remote_job_id=job.remote_job_id,
            provider_artifact_reference=artifact_reference,
            submitted_at=(job.submitted_at or run.submitted_at),
            started_at=(job.started_at or run.started_at),
            completed_at=(job.completed_at if target in {
                TrainingRunStatus.SUCCEEDED,
                TrainingRunStatus.FAILED,
                TrainingRunStatus.CANCELED,
                TrainingRunStatus.UNSUPPORTED,
            } else None),
            failure_code=run_failure_code,
        )
        row.status = target.value
        row.remote_job_id = updated.remote_job_id
        row.run_json = _training_run_payload(updated)
        row.submitted_at = updated.submitted_at
        row.started_at = updated.started_at
        row.completed_at = updated.completed_at

    @staticmethod
    def _register_model_artifact(
        session: Session, job: TrainingJob, artifact: TrainingArtifact
    ) -> None:
        run_row = session.scalar(
            select(TrainingRunRow).where(
                TrainingRunRow.tenant_id == job.tenant_id,
                TrainingRunRow.training_run_id == job.training_run_id,
            )
        )
        if run_row is None:
            raise ResourceConflictError("Successful job has no TrainingRun")
        run = _training_run_from_payload(run_row.run_json)
        model_version = session.get(ModelVersionRow, run.candidate_model_version_id)
        if model_version is None:
            session.add(
                ModelVersionRow(
                    model_version_id=run.candidate_model_version_id,
                    tenant_id=job.tenant_id,
                    version=run.candidate_model_version,
                    created_at=artifact.created_at,
                )
            )
        model_artifact = ModelArtifact(
            artifact_id=artifact.artifact_id,
            tenant_id=job.tenant_id,
            training_run_id=job.training_run_id,
            model_version_id=run.candidate_model_version_id,
            model_version=run.candidate_model_version,
            target_type=run.target_type,
            provider=run.provider,
            provider_artifact_reference=artifact.source_uri,
            base_model=run.base_model,
            base_model_version=run.base_model_version,
            training_dataset_id=run.training_dataset_id,
            training_dataset_version=run.training_dataset_version,
            code_version=run.code_version,
            stage=ModelLifecycleStage.REGISTERED,
            created_at=artifact.created_at,
        )
        existing = session.get(ModelArtifactRow, artifact.artifact_id)
        payload = _model_artifact_payload(model_artifact)
        if existing is not None:
            if existing.artifact_json != payload:
                raise ResourceConflictError("Model artifact identity is immutable")
            return
        session.add(
            ModelArtifactRow(
                artifact_id=model_artifact.artifact_id,
                tenant_id=model_artifact.tenant_id,
                training_run_key=run_row.training_run_key,
                model_version_id=model_artifact.model_version_id,
                target_type=model_artifact.target_type.value,
                provider=model_artifact.provider,
                stage=model_artifact.stage.value,
                is_valid=model_artifact.is_valid,
                artifact_json=payload,
                created_at=model_artifact.created_at,
                invalidated_at=model_artifact.invalidated_at,
            )
        )


def _job_row(
    job: TrainingJob, idempotency_digest: str, run_row: TrainingRunRow | None
) -> TrainingJobRow:
    return TrainingJobRow(
        job_id=job.job_id,
        tenant_id=job.tenant_id,
        dataset_export_id=job.dataset_export_id,
        training_run_key=run_row.training_run_key if run_row else None,
        training_run_id=job.training_run_id,
        provider=job.provider,
        status=job.status.value,
        remote_job_id=job.remote_job_id,
        retry_of_job_id=job.retry_of_job_id,
        idempotency_digest=idempotency_digest,
        operation_submit_id=f"submit:{job.job_id}",
        operation_cancel_id=f"cancel:{job.job_id}",
        operation_commit_id=f"commit:{job.job_id}",
        revision=job.revision,
        claim_count=job.claim_count,
        failure_attempt_count=job.failure_attempt_count,
        next_attempt_at=job.next_attempt_at,
        worker_id=job.worker_id,
        lease_token=job.lease_token,
        lease_expires_at=job.lease_expires_at,
        cancel_requested_at=job.cancel_requested_at,
        cancel_requested_by=job.cancel_requested_by,
        cancel_reason=job.cancel_reason,
        job_json=_job_payload(job),
        created_at=job.created_at,
        updated_at=job.updated_at,
        completed_at=job.completed_at,
    )


def _job_payload(job: TrainingJob) -> dict[str, Any]:
    payload = asdict(job)
    payload["status"] = job.status.value
    for key, value in tuple(payload.items()):
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    return payload


def _job_from_row(row: TrainingJobRow) -> TrainingJob:
    payload = dict(row.job_json)
    payload["status"] = TrainingJobStatus(str(payload["status"]))
    for key in (
        "created_at", "updated_at", "cancel_requested_at", "next_attempt_at",
        "lease_expires_at", "submitted_at", "started_at", "completed_at",
    ):
        if payload.get(key) is not None:
            payload[key] = datetime.fromisoformat(str(payload[key]))
    return TrainingJob(**payload)


def _export_payload(value: TrainingDatasetExport) -> dict[str, Any]:
    payload = asdict(value)
    payload["created_at"] = value.created_at.isoformat()
    return payload


def _export_from_payload(payload: dict[str, Any]) -> TrainingDatasetExport:
    values = dict(payload)
    values["created_at"] = datetime.fromisoformat(str(values["created_at"]))
    return TrainingDatasetExport(**values)


def _export_row(value: TrainingDatasetExport, dataset_key: str) -> TrainingDatasetExportRow:
    return TrainingDatasetExportRow(
        export_id=value.export_id,
        dataset_key=dataset_key,
        tenant_scope=value.tenant_scope,
        dataset_id=value.dataset_id,
        dataset_version=value.dataset_version,
        schema_version=value.schema_version,
        manifest_uri=value.manifest_uri,
        manifest_checksum_sha256=value.manifest_checksum_sha256,
        record_count=value.record_count,
        positive_count=value.positive_count,
        hard_negative_count=value.hard_negative_count,
        created_by=value.created_by,
        export_json=_export_payload(value),
        created_at=value.created_at,
    )


def _artifact_payload(value: TrainingArtifact) -> dict[str, Any]:
    payload = asdict(value)
    payload["created_at"] = value.created_at.isoformat()
    return payload


def _immutable_job(job: TrainingJob) -> tuple[object, ...]:
    return (
        job.tenant_id, job.dataset_export_id, job.tenant_scope, job.dataset_id,
        job.dataset_version, job.schema_version, job.index_version, job.model_version,
        job.prompt_version, job.provider, job.target_type, job.base_model,
        job.base_model_version, job.code_version, job.training_run_id,
    )


def _dataset_key(tenant_scope: str, dataset_id: str, version: str) -> str:
    return sha256(f"{tenant_scope}\0{dataset_id}\0{version}".encode()).hexdigest()


def _safe_code(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in value.strip().lower()
    )
    return (normalized or "unknown")[:128]
