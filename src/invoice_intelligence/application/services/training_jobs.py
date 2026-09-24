"""Tenant-scoped Training Job commands used by FastAPI."""

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256

from invoice_intelligence.application.errors import (
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.ports.training import TrainingRunRepository
from invoice_intelligence.application.ports.training_registry import (
    TrainingRegistryRepository,
)
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.training import (
    TrainingRun,
    TrainingRunStatus,
    TrainingTargetType,
)
from invoice_intelligence.domain.training_registry import (
    TERMINAL_TRAINING_JOB_STATUSES,
    TrainingDatasetExport,
    TrainingJob,
    TrainingJobStatus,
)


@dataclass(frozen=True, slots=True)
class CreateTrainingJobCommand:
    dataset_export_id: str
    tenant_scope: str
    dataset_id: str
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    target_type: TrainingTargetType
    base_model: str
    base_model_version: str
    code_version: str
    manifest_uri: str
    manifest_checksum_sha256: str
    record_count: int
    positive_count: int
    hard_negative_count: int


class TrainingJobService:
    """Register intent only; this service has no TrainingProvider dependency."""

    def __init__(
        self,
        *,
        registry: TrainingRegistryRepository,
        run_repository: TrainingRunRepository,
        provider_name: str,
    ) -> None:
        self._registry = registry
        self._runs = run_repository
        self._provider_name = provider_name

    async def create(
        self,
        context: TrustedTenantContext,
        command: CreateTrainingJobCommand,
        *,
        idempotency_key: str,
    ) -> TrainingJob:
        now = datetime.now(UTC)
        job_id = _stable_id("training-job", context.tenant_id, idempotency_key)
        training_run_id = _stable_id("training-run", context.tenant_id, job_id)
        export = TrainingDatasetExport(
            export_id=command.dataset_export_id,
            tenant_scope=command.tenant_scope,
            dataset_id=command.dataset_id,
            dataset_version=command.dataset_version,
            schema_version=command.schema_version,
            manifest_uri=command.manifest_uri,
            manifest_checksum_sha256=command.manifest_checksum_sha256,
            record_count=command.record_count,
            positive_count=command.positive_count,
            hard_negative_count=command.hard_negative_count,
            created_by=context.actor_id,
            created_at=now,
        )
        run = TrainingRun(
            training_run_id=training_run_id,
            tenant_id=context.tenant_id,
            target_type=command.target_type,
            provider=self._provider_name,
            base_model=command.base_model,
            base_model_version=command.base_model_version,
            candidate_model_version_id=_stable_id(
                "model-version", context.tenant_id, command.model_version
            ),
            candidate_model_version=command.model_version,
            training_dataset_tenant_scope=command.tenant_scope,
            training_dataset_id=command.dataset_id,
            training_dataset_version=command.dataset_version,
            validation_dataset_id=command.dataset_id,
            validation_dataset_version=command.dataset_version,
            evaluation_dataset_id=command.dataset_id,
            evaluation_dataset_version=command.dataset_version,
            schema_version=command.schema_version,
            hyperparameters=(),
            code_version=command.code_version,
            training_artifact_references=(command.manifest_uri,),
            status=TrainingRunStatus.EXPORT_READY,
            created_at=now,
        )
        await self._runs.save_training_run(run)
        job = TrainingJob(
            job_id=job_id,
            tenant_id=context.tenant_id,
            dataset_export_id=command.dataset_export_id,
            tenant_scope=command.tenant_scope,
            dataset_id=command.dataset_id,
            dataset_version=command.dataset_version,
            schema_version=command.schema_version,
            index_version=command.index_version,
            model_version=command.model_version,
            prompt_version=command.prompt_version,
            provider=self._provider_name,
            target_type=command.target_type.value,
            base_model=command.base_model,
            base_model_version=command.base_model_version,
            code_version=command.code_version,
            training_run_id=training_run_id,
            status=TrainingJobStatus.PLANNED,
            requested_by=context.actor_id,
            created_at=now,
            updated_at=now,
            trace_id=context.trace_id,
        )
        return await self._registry.create_job(
            job,
            idempotency_digest=_idempotency_digest("create", idempotency_key),
            dataset_export=export,
        )

    async def get(self, context: TrustedTenantContext, job_id: str) -> TrainingJob:
        job = await self._registry.get_job(context.tenant_id, job_id)
        if job is None:
            raise ResourceNotFoundError("Training job was not found")
        return job

    async def cancel(
        self,
        context: TrustedTenantContext,
        job_id: str,
        *,
        reason: str,
        idempotency_key: str,
    ) -> TrainingJob:
        await self.get(context, job_id)
        return await self._registry.request_cancel(
            context.tenant_id,
            job_id,
            actor_id=context.actor_id,
            reason=reason,
            idempotency_digest=_idempotency_digest(
                f"cancel:{job_id}", idempotency_key
            ),
        )

    async def retry(
        self,
        context: TrustedTenantContext,
        job_id: str,
        *,
        idempotency_key: str,
    ) -> TrainingJob:
        source = await self.get(context, job_id)
        if source.status not in TERMINAL_TRAINING_JOB_STATUSES:
            raise ResourceConflictError("Only terminal training jobs can be retried")
        now = datetime.now(UTC)
        retry_id = _stable_id("training-job-retry", source.job_id, idempotency_key)
        run_id = _stable_id("training-run", context.tenant_id, retry_id)
        old_run = await self._runs.get_training_run(context.tenant_id, source.training_run_id)
        if old_run is None:
            raise ResourceConflictError("Source training run is unavailable")
        await self._runs.save_training_run(
            replace(
                old_run,
                training_run_id=run_id,
                candidate_model_version_id=_stable_id(
                    "model-version", context.tenant_id, f"{source.model_version}:{retry_id}"
                ),
                status=TrainingRunStatus.EXPORT_READY,
                created_at=now,
                remote_job_id=None,
                provider_artifact_reference=None,
                submitted_at=None,
                started_at=None,
                completed_at=None,
                failure_code=None,
            )
        )
        retry = replace(
            source,
            job_id=retry_id,
            training_run_id=run_id,
            status=TrainingJobStatus.PLANNED,
            requested_by=context.actor_id,
            created_at=now,
            updated_at=now,
            revision=1,
            remote_job_id=None,
            retry_of_job_id=source.job_id,
            cancel_requested_at=None,
            cancel_requested_by=None,
            cancel_reason=None,
            claim_count=0,
            failure_attempt_count=0,
            next_attempt_at=None,
            worker_id=None,
            lease_token=None,
            lease_expires_at=None,
            submitted_at=None,
            started_at=None,
            completed_at=None,
            failure_code=None,
            trace_id=context.trace_id,
        )
        return await self._registry.create_retry(
            source,
            retry,
            idempotency_digest=_idempotency_digest(
                f"retry:{job_id}", idempotency_key
            ),
        )


def _stable_id(namespace: str, *values: str) -> str:
    return sha256("\0".join((namespace, *values)).encode("utf-8")).hexdigest()


def _idempotency_digest(operation: str, value: str) -> str:
    return sha256(f"{operation}\0{value}".encode()).hexdigest()
