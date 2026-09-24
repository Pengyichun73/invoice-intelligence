from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from invoice_intelligence.api.dependencies import (
    get_training_job_service,
    get_trusted_tenant_context,
)
from invoice_intelligence.api.routes.training import router
from invoice_intelligence.application.services.training_jobs import (
    CreateTrainingJobCommand,
)
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.training_registry import TrainingJob, TrainingJobStatus


class _TrainingService:
    def __init__(self) -> None:
        self.command: CreateTrainingJobCommand | None = None

    async def create(
        self,
        context: TrustedTenantContext,
        command: CreateTrainingJobCommand,
        *,
        idempotency_key: str,
    ) -> TrainingJob:
        assert context.tenant_id == "tenant-a"
        assert idempotency_key == "request-1"
        self.command = command
        now = datetime.now(UTC)
        return TrainingJob(
            job_id="j" * 64,
            tenant_id=context.tenant_id,
            dataset_export_id=command.dataset_export_id,
            tenant_scope=command.tenant_scope,
            dataset_id=command.dataset_id,
            dataset_version=command.dataset_version,
            schema_version=command.schema_version,
            index_version=command.index_version,
            model_version=command.model_version,
            prompt_version=command.prompt_version,
            provider="stub",
            target_type=command.target_type.value,
            base_model=command.base_model,
            base_model_version=command.base_model_version,
            code_version=command.code_version,
            training_run_id="r" * 64,
            status=TrainingJobStatus.PLANNED,
            requested_by=context.actor_id,
            created_at=now,
            updated_at=now,
        )


def test_create_api_only_registers_training_intent() -> None:
    service = _TrainingService()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_training_job_service] = lambda: service
    app.dependency_overrides[get_trusted_tenant_context] = lambda: TrustedTenantContext(
        tenant_id="tenant-a",
        actor_id="reviewer-a",
        permissions=frozenset(),
        trace_id="trace-1",
    )
    response = TestClient(app).post(
        "/api/v1/training/jobs",
        headers={"Idempotency-Key": "request-1"},
        json={
            "dataset_export_id": "e" * 64,
            "tenant_scope": "scope-a",
            "dataset_id": "dataset-a",
            "dataset_version": "v1",
            "schema_version": "3.0.0",
            "index_version": "index-v1",
            "model_version": "model-v1",
            "prompt_version": "langsmith:prompt:v1",
            "target_type": "reranker",
            "base_model": "base",
            "base_model_version": "base-v1",
            "code_version": "code-v1",
            "manifest_uri": "https://artifacts.example/dataset.jsonl",
            "manifest_checksum_sha256": "a" * 64,
            "record_count": 1,
            "positive_count": 1,
            "hard_negative_count": 0,
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "planned"
    assert service.command is not None
    assert "tenant_id" not in service.command.__dataclass_fields__
