"""训练 Stub 与可选远程 Adapter 仅返回脱敏、可分类的失败。"""

from datetime import UTC, datetime

import httpx
import pytest

from invoice_intelligence.application.errors import (
    TrainingProviderPermanentError,
    TrainingProviderUnavailableError,
)
from invoice_intelligence.application.services.training_worker import (
    TrainingWorkerConfig,
    TrainingWorkerService,
)
from invoice_intelligence.domain.training_registry import (
    TrainingDatasetExport,
    TrainingJob,
    TrainingJobLease,
    TrainingJobStatus,
    TrainingProviderRequest,
)
from invoice_intelligence.infrastructure.training.mlflow_compatible import (
    MLflowCompatibleTrainingProvider,
)
from invoice_intelligence.infrastructure.training.stub import (
    DeterministicStubTrainingProvider,
)


def _request() -> TrainingProviderRequest:
    return TrainingProviderRequest(
        job_id="job-1", tenant_id="tenant-a", target_type="reranker",
        base_model="synthetic-base", base_model_version="v1",
        dataset_manifest_uri="isolated://manifest",
        dataset_manifest_checksum_sha256="a" * 64,
        dataset_version="dataset-v1", schema_version="3.0.0",
        index_version="index-v1", model_version="model-v1",
        prompt_version="prompt-v1", code_version="code-v1",
    )


class _Registry:
    def __init__(self) -> None:
        self.advanced = None
        self.rescheduled = None

    async def get_dataset_export(self, _export_id):
        return TrainingDatasetExport(
            export_id="export-1", tenant_scope="tenant-a",
            dataset_id="dataset-1", dataset_version="dataset-v1",
            schema_version="3.0.0", manifest_uri="isolated://manifest",
            manifest_checksum_sha256="a" * 64, record_count=1,
            positive_count=1, hard_negative_count=0,
            created_by="reviewer-a", created_at=datetime(2026, 9, 24, tzinfo=UTC),
        )

    async def advance(self, _lease, *, status, failure_code=None, **_kwargs):
        self.advanced = (status, failure_code)

    async def reschedule(self, _lease, *, error_code, delay_seconds, max_attempts):
        self.rescheduled = (error_code, delay_seconds, max_attempts)


def _lease() -> TrainingJobLease:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    job = TrainingJob(
        job_id="job-1", tenant_id="tenant-a", dataset_export_id="export-1",
        tenant_scope="tenant-a", dataset_id="dataset-1",
        dataset_version="dataset-v1", schema_version="3.0.0",
        index_version="index-v1", model_version="model-v1",
        prompt_version="prompt-v1", provider="stub", target_type="reranker",
        base_model="synthetic-base", base_model_version="v1",
        code_version="code-v1", training_run_id="run-1",
        status=TrainingJobStatus.PLANNED, requested_by="reviewer-a",
        created_at=now, updated_at=now,
    )
    return TrainingJobLease(job=job, worker_id="worker-a", lease_token="lease-a", revision=1)


@pytest.mark.asyncio
async def test_default_stub_never_reports_training_success() -> None:
    provider = DeterministicStubTrainingProvider()
    with pytest.raises(TrainingProviderPermanentError, match="not_configured"):
        await provider.submit(_request(), operation_id="submit:job-1")
    with pytest.raises(TrainingProviderPermanentError, match="not_configured"):
        await provider.get_job("remote-1")
    with pytest.raises(TrainingProviderPermanentError, match="not_configured"):
        await provider.cancel("remote-1", operation_id="cancel:job-1")


@pytest.mark.asyncio
async def test_training_worker_quarantines_unconfigured_stub_without_retry() -> None:
    registry = _Registry()
    worker = TrainingWorkerService(
        config=TrainingWorkerConfig(worker_id="worker-a"),
        registry=registry, provider=DeterministicStubTrainingProvider(),
        artifact_reader=object(),
    )
    await worker.process(_lease())
    assert registry.advanced == (
        TrainingJobStatus.QUARANTINED, "training_provider_not_configured",
    )
    assert registry.rescheduled is None


@pytest.mark.asyncio
async def test_training_worker_retries_remote_rate_limit() -> None:
    registry = _Registry()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(429))
    ) as client:
        provider = MLflowCompatibleTrainingProvider(
            base_url="https://training.example.test", submit_path="/jobs",
            job_path_template="/jobs/{job_id}",
            cancel_path_template="/jobs/{job_id}/cancel", bearer_token="synthetic-token",
            timeout_seconds=2, client=client,
        )
        worker = TrainingWorkerService(
            config=TrainingWorkerConfig(worker_id="worker-a"),
            registry=registry, provider=provider, artifact_reader=object(),
        )
        await worker.process(_lease())
    assert registry.advanced is None
    assert registry.rescheduled == ("training_provider_unavailable", 2.0, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(400, TrainingProviderPermanentError),
     (429, TrainingProviderUnavailableError),
     (503, TrainingProviderUnavailableError)],
)
async def test_remote_http_failures_are_classified_without_response_body(
    status_code: int, error_type: type[Exception],
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "submit:job-1"
        return httpx.Response(status_code, text="SENSITIVE_REMOTE_BODY")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = MLflowCompatibleTrainingProvider(
            base_url="https://training.example.test", submit_path="/jobs",
            job_path_template="/jobs/{job_id}",
            cancel_path_template="/jobs/{job_id}/cancel", bearer_token="synthetic-token",
            timeout_seconds=2, client=client,
        )
        with pytest.raises(error_type) as caught:
            await provider.submit(_request(), operation_id="submit:job-1")
    assert "SENSITIVE_REMOTE_BODY" not in str(caught.value)
    assert "synthetic-token" not in str(caught.value)


@pytest.mark.asyncio
async def test_remote_malformed_response_is_permanent_contract_failure() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={"status": "ok"}))
    ) as client:
        provider = MLflowCompatibleTrainingProvider(
            base_url="https://training.example.test", submit_path="/jobs",
            job_path_template="/jobs/{job_id}",
            cancel_path_template="/jobs/{job_id}/cancel", bearer_token="synthetic-token",
            timeout_seconds=2, client=client,
        )
        with pytest.raises(TrainingProviderPermanentError, match="contract_invalid"):
            await provider.submit(_request(), operation_id="submit:job-1")
