"""Application boundaries for asynchronous training control-plane operations."""

from collections.abc import Sequence
from typing import Protocol

from invoice_intelligence.domain.training_registry import (
    TrainingArtifact,
    TrainingDatasetExport,
    TrainingJob,
    TrainingJobLease,
    TrainingJobStatus,
    TrainingProviderRequest,
    TrainingProviderSnapshot,
)


class TrainingProvider(Protocol):
    async def submit(
        self, request: TrainingProviderRequest, *, operation_id: str
    ) -> TrainingProviderSnapshot: ...

    async def get_job(self, provider_job_id: str) -> TrainingProviderSnapshot: ...

    async def cancel(
        self, provider_job_id: str, *, operation_id: str
    ) -> TrainingProviderSnapshot: ...


class TrainingArtifactReader(Protocol):
    async def read_and_verify(
        self, uri: str, *, expected_sha256: str
    ) -> tuple[int, str]:
        """Return byte length and verified SHA-256 without exposing artifact bytes."""

        ...


class TrainingRegistryRepository(Protocol):
    async def create_job(
        self,
        job: TrainingJob,
        *,
        idempotency_digest: str,
        dataset_export: TrainingDatasetExport,
    ) -> TrainingJob: ...

    async def get_job(self, tenant_id: str, job_id: str) -> TrainingJob | None: ...

    async def request_cancel(
        self,
        tenant_id: str,
        job_id: str,
        *,
        actor_id: str,
        reason: str,
        idempotency_digest: str,
    ) -> TrainingJob: ...

    async def create_retry(
        self,
        source: TrainingJob,
        retry: TrainingJob,
        *,
        idempotency_digest: str,
    ) -> TrainingJob: ...

    async def claim_jobs(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_seconds: float,
    ) -> Sequence[TrainingJobLease]: ...

    async def get_dataset_export(self, export_id: str) -> TrainingDatasetExport | None: ...

    async def advance(
        self,
        lease: TrainingJobLease,
        *,
        status: TrainingJobStatus,
        remote_job_id: str | None = None,
        failure_code: str | None = None,
    ) -> TrainingJob: ...

    async def reschedule(
        self,
        lease: TrainingJobLease,
        *,
        error_code: str,
        delay_seconds: float,
        max_attempts: int,
    ) -> TrainingJob: ...

    async def release(self, lease: TrainingJobLease) -> None: ...

    async def commit_success(
        self,
        lease: TrainingJobLease,
        artifact: TrainingArtifact,
    ) -> TrainingJob: ...
