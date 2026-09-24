from datetime import UTC, datetime

import pytest

from invoice_intelligence.application.errors import TrainingProviderPermanentError
from invoice_intelligence.application.services.training_worker import (
    TrainingWorkerConfig,
    TrainingWorkerService,
)
from invoice_intelligence.domain.training_registry import (
    TrainingDatasetExport,
    TrainingJob,
    TrainingJobLease,
    TrainingJobStatus,
)
from invoice_intelligence.infrastructure.training.stub import (
    DeterministicStubTrainingProvider,
)


def _job(*, status: TrainingJobStatus = TrainingJobStatus.PLANNED) -> TrainingJob:
    now = datetime.now(UTC)
    return TrainingJob(
        job_id="j" * 64,
        tenant_id="tenant-a",
        dataset_export_id="e" * 64,
        tenant_scope="scope-a",
        dataset_id="dataset-a",
        dataset_version="dataset-v1",
        schema_version="3.0.0",
        index_version="index-v1",
        model_version="model-v1",
        prompt_version="langsmith:prompt:v1",
        provider="stub",
        target_type="reranker",
        base_model="base",
        base_model_version="base-v1",
        code_version="code-v1",
        training_run_id="r" * 64,
        status=status,
        requested_by="reviewer-a",
        created_at=now,
        updated_at=now,
    )


def test_training_job_has_exact_seven_states_and_terminal_jobs_are_immutable() -> None:
    assert {item.value for item in TrainingJobStatus} == {
        "planned",
        "submitted",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "quarantined",
    }
    job = _job()
    assert job.can_transition_to(TrainingJobStatus.SUBMITTED)
    assert not job.can_transition_to(TrainingJobStatus.SUCCEEDED)


@pytest.mark.asyncio
async def test_stub_never_reports_remote_success() -> None:
    provider = DeterministicStubTrainingProvider()
    with pytest.raises(TrainingProviderPermanentError):
        await provider.get_job("remote-job")


class _Registry:
    def __init__(self, export: TrainingDatasetExport) -> None:
        self.export = export
        self.advanced: tuple[TrainingJobStatus, str | None] | None = None

    async def get_dataset_export(self, export_id: str) -> TrainingDatasetExport | None:
        return self.export if export_id == self.export.export_id else None

    async def advance(
        self,
        lease: TrainingJobLease,
        *,
        status: TrainingJobStatus,
        remote_job_id: str | None = None,
        failure_code: str | None = None,
    ) -> TrainingJob:
        del lease, remote_job_id
        self.advanced = (status, failure_code)
        return _job()


class _UnusedArtifactReader:
    async def read_and_verify(
        self, uri: str, *, expected_sha256: str
    ) -> tuple[int, str]:
        raise AssertionError(f"unexpected artifact read: {uri} {expected_sha256}")


@pytest.mark.asyncio
async def test_worker_quarantines_unconfigured_stub_without_fake_success() -> None:
    now = datetime.now(UTC)
    export = TrainingDatasetExport(
        export_id="e" * 64,
        tenant_scope="scope-a",
        dataset_id="dataset-a",
        dataset_version="dataset-v1",
        schema_version="3.0.0",
        manifest_uri="https://artifacts.example/dataset.jsonl",
        manifest_checksum_sha256="a" * 64,
        record_count=1,
        positive_count=1,
        hard_negative_count=1,
        created_by="reviewer-a",
        created_at=now,
    )
    registry = _Registry(export)
    job = _job()
    lease = TrainingJobLease(job=job, worker_id="worker-a", lease_token="b" * 64, revision=1)
    worker = TrainingWorkerService(
        config=TrainingWorkerConfig(worker_id="worker-a"),
        registry=registry,  # type: ignore[arg-type]
        provider=DeterministicStubTrainingProvider(),
        artifact_reader=_UnusedArtifactReader(),
    )

    await worker.process(lease)

    assert registry.advanced == (
        TrainingJobStatus.QUARANTINED,
        "training_provider_not_configured",
    )
