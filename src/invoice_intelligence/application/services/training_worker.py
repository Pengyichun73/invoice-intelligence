"""Lease-based worker orchestration for asynchronous training jobs."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from invoice_intelligence.application.errors import (
    TrainingProviderPermanentError,
    TrainingProviderUnavailableError,
)
from invoice_intelligence.application.ports.training_registry import (
    TrainingArtifactReader,
    TrainingProvider,
    TrainingRegistryRepository,
)
from invoice_intelligence.domain.training_registry import (
    TrainingArtifact,
    TrainingJobLease,
    TrainingJobStatus,
    TrainingProviderJobStatus,
    TrainingProviderRequest,
    TrainingProviderSnapshot,
)


@dataclass(frozen=True, slots=True)
class TrainingWorkerConfig:
    worker_id: str
    poll_interval_seconds: float = 2.0
    batch_size: int = 8
    lease_seconds: float = 300.0
    max_attempts: int = 5
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 300.0


class TrainingWorkerService:
    def __init__(
        self,
        *,
        config: TrainingWorkerConfig,
        registry: TrainingRegistryRepository,
        provider: TrainingProvider,
        artifact_reader: TrainingArtifactReader,
    ) -> None:
        self._config = config
        self._registry = registry
        self._provider = provider
        self._artifacts = artifact_reader

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            leases = await self._registry.claim_jobs(
                worker_id=self._config.worker_id,
                limit=self._config.batch_size,
                lease_seconds=self._config.lease_seconds,
            )
            if not leases:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=self._config.poll_interval_seconds
                    )
                except TimeoutError:
                    pass
                continue
            await asyncio.gather(*(self.process(lease) for lease in leases))

    async def process(self, lease: TrainingJobLease) -> None:
        try:
            job = lease.job
            if job.cancel_requested_at is not None:
                await self._cancel(lease)
                return
            if job.status is TrainingJobStatus.PLANNED:
                await self._submit(lease)
                return
            if job.remote_job_id is None:
                raise TrainingProviderPermanentError("remote_job_identity_missing")
            snapshot = await self._provider.get_job(job.remote_job_id)
            await self._apply_snapshot(lease, snapshot)
        except TrainingProviderUnavailableError as exc:
            await self._retry(lease, str(exc))
        except TrainingProviderPermanentError as exc:
            await self._registry.advance(
                lease,
                status=TrainingJobStatus.QUARANTINED,
                failure_code=_safe_code(str(exc)),
            )

    async def _submit(self, lease: TrainingJobLease) -> None:
        export = await self._registry.get_dataset_export(lease.job.dataset_export_id)
        if export is None:
            raise TrainingProviderPermanentError("dataset_export_missing")
        job = lease.job
        snapshot = await self._provider.submit(
            TrainingProviderRequest(
                job_id=job.job_id,
                tenant_id=job.tenant_id,
                target_type=job.target_type,
                base_model=job.base_model,
                base_model_version=job.base_model_version,
                dataset_manifest_uri=export.manifest_uri,
                dataset_manifest_checksum_sha256=export.manifest_checksum_sha256,
                dataset_version=job.dataset_version,
                schema_version=job.schema_version,
                index_version=job.index_version,
                model_version=job.model_version,
                prompt_version=job.prompt_version,
                code_version=job.code_version,
            ),
            operation_id=f"submit:{job.job_id}",
        )
        # Submission may complete remotely immediately, but local state first records
        # the stable remote identity. A later refresh reconciles the terminal result.
        await self._registry.advance(
            lease,
            status=TrainingJobStatus.SUBMITTED,
            remote_job_id=snapshot.provider_job_id,
        )

    async def _cancel(self, lease: TrainingJobLease) -> None:
        job = lease.job
        if job.remote_job_id is None:
            await self._registry.advance(lease, status=TrainingJobStatus.CANCELLED)
            return
        snapshot = await self._provider.cancel(
            job.remote_job_id, operation_id=f"cancel:{job.job_id}"
        )
        if snapshot.status is TrainingProviderJobStatus.SUCCEEDED:
            await self._apply_snapshot(lease, snapshot)
        elif snapshot.status is TrainingProviderJobStatus.CANCELLED:
            await self._registry.advance(
                lease,
                status=TrainingJobStatus.CANCELLED,
                remote_job_id=snapshot.provider_job_id,
            )
        else:
            await self._registry.release(lease)

    async def _apply_snapshot(
        self, lease: TrainingJobLease, snapshot: TrainingProviderSnapshot
    ) -> None:
        if lease.job.remote_job_id != snapshot.provider_job_id:
            raise TrainingProviderPermanentError("remote_job_identity_changed")
        if snapshot.status is TrainingProviderJobStatus.RUNNING:
            await self._registry.advance(
                lease,
                status=TrainingJobStatus.RUNNING,
                remote_job_id=snapshot.provider_job_id,
            )
            return
        if snapshot.status is TrainingProviderJobStatus.FAILED:
            await self._registry.advance(
                lease,
                status=TrainingJobStatus.FAILED,
                remote_job_id=snapshot.provider_job_id,
                failure_code=_safe_code(snapshot.failure_code or "remote_training_failed"),
            )
            return
        if snapshot.status is TrainingProviderJobStatus.CANCELLED:
            await self._registry.advance(
                lease,
                status=TrainingJobStatus.CANCELLED,
                remote_job_id=snapshot.provider_job_id,
            )
            return
        if snapshot.status is TrainingProviderJobStatus.SUBMITTED:
            await self._registry.release(lease)
            return
        assert snapshot.artifact_uri is not None
        assert snapshot.artifact_checksum_sha256 is not None
        size, checksum = await self._artifacts.read_and_verify(
            snapshot.artifact_uri,
            expected_sha256=snapshot.artifact_checksum_sha256,
        )
        export = await self._registry.get_dataset_export(lease.job.dataset_export_id)
        if export is None:
            raise TrainingProviderPermanentError("dataset_export_missing")
        job = lease.job
        artifact = TrainingArtifact(
            artifact_id=sha256(f"{job.tenant_id}\0{job.job_id}\0{checksum}".encode()).hexdigest(),
            tenant_id=job.tenant_id,
            job_id=job.job_id,
            training_run_id=job.training_run_id,
            remote_job_id=snapshot.provider_job_id,
            provider=job.provider,
            source_uri=snapshot.artifact_uri,
            checksum_sha256=checksum,
            size_bytes=size,
            dataset_id=job.dataset_id,
            dataset_version=job.dataset_version,
            dataset_manifest_checksum_sha256=export.manifest_checksum_sha256,
            schema_version=job.schema_version,
            index_version=job.index_version,
            model_version=job.model_version,
            prompt_version=job.prompt_version,
            code_version=job.code_version,
            created_by=self._config.worker_id,
            created_at=datetime.now(UTC),
            trace_id=job.trace_id,
        )
        await self._registry.commit_success(lease, artifact)

    async def _retry(self, lease: TrainingJobLease, error: str) -> None:
        attempt = lease.job.failure_attempt_count + 1
        delay = min(
            self._config.backoff_base_seconds * (2 ** max(0, attempt - 1)),
            self._config.backoff_max_seconds,
        )
        await self._registry.reschedule(
            lease,
            error_code=_safe_code(error),
            delay_seconds=delay,
            max_attempts=self._config.max_attempts,
        )


def _safe_code(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {".", "_", "-"} else "_"
        for character in value.strip().lower()
    )
    return (normalized or "training_error")[:128]
