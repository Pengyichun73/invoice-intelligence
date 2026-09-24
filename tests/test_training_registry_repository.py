from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import ResourceNotFoundError
from invoice_intelligence.application.services.training_jobs import TrainingJobService
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.training import (
    TrainingRun,
    TrainingRunStatus,
    TrainingTargetType,
)
from invoice_intelligence.domain.training_registry import (
    TrainingDatasetExport,
    TrainingJob,
    TrainingJobStatus,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_model_training import (
    SQLAlchemyModelTrainingRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    TrainingArtifactRow,
    TrainingAuditEventRow,
    TrainingDatasetExportRow,
    TrainingDatasetRecordRow,
    TrainingDatasetVersionRow,
    TrainingJobRow,
    TrainingRunRow,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_training_registry import (
    SQLAlchemyTrainingRegistryRepository,
    _dataset_key,
)


@pytest.mark.asyncio
async def test_repository_is_tenant_scoped_and_claims_planned_job() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            TrainingDatasetVersionRow.__table__,
            TrainingDatasetExportRow.__table__,
            TrainingDatasetRecordRow.__table__,
            TrainingRunRow.__table__,
            TrainingJobRow.__table__,
            TrainingArtifactRow.__table__,
            TrainingAuditEventRow.__table__,
        ],
    )
    now = datetime.now(UTC)
    dataset_key = _dataset_key("scope-a", "dataset-a", "v1")
    with Session(engine) as session:
        session.add(
            TrainingDatasetVersionRow(
                dataset_key=dataset_key,
                tenant_scope="scope-a",
                dataset_id="dataset-a",
                dataset_version="v1",
                source_tenant_ids_json=["tenant-a"],
                schema_version="3.0.0",
                generation_rule_version="generation-v1",
                redaction_policy_version="redaction-v1",
                split_rule_version="split-v1",
                split_salt_version="salt-v1",
                created_by="reviewer-a",
                cross_tenant=False,
                authorization_id=None,
                status="exported",
                record_count=1,
                fingerprint="f" * 64,
                dataset_json={},
                created_at=now,
                completed_at=now,
            )
        )
        session.add(
            TrainingDatasetRecordRow(
                record_id="record-1",
                dataset_key=dataset_key,
                source_tenant_id="tenant-a",
                split="train",
                source_document_ids_json=["document-1"],
                candidate_ids_json=["candidate-1"],
                group_fingerprint="g" * 64,
                fingerprint="r" * 64,
                record_json={
                    "record": {
                        "query": "redacted query",
                        "positive": "redacted positive",
                        "hard_negatives": [],
                    }
                },
                created_at=now,
            )
        )
        session.commit()
    export = TrainingDatasetExport(
        export_id="e" * 64,
        tenant_scope="scope-a",
        dataset_id="dataset-a",
        dataset_version="v1",
        schema_version="3.0.0",
        manifest_uri="https://artifacts.example/dataset.jsonl",
        manifest_checksum_sha256="a" * 64,
        record_count=1,
        positive_count=1,
        hard_negative_count=0,
        created_by="reviewer-a",
        created_at=now,
    )
    job = TrainingJob(
        job_id="j" * 64,
        tenant_id="tenant-a",
        dataset_export_id=export.export_id,
        tenant_scope="scope-a",
        dataset_id="dataset-a",
        dataset_version="v1",
        schema_version="3.0.0",
        index_version="index-v1",
        model_version="model-v1",
        prompt_version="prompt-v1",
        provider="stub",
        target_type="reranker",
        base_model="base",
        base_model_version="base-v1",
        code_version="code-v1",
        training_run_id="r" * 64,
        status=TrainingJobStatus.PLANNED,
        requested_by="reviewer-a",
        created_at=now,
        updated_at=now,
    )
    repository = SQLAlchemyTrainingRegistryRepository(engine)

    created = await repository.create_job(
        job, idempotency_digest="i" * 64, dataset_export=export
    )

    assert created == job
    assert await repository.get_job("tenant-b", job.job_id) is None
    service = TrainingJobService(
        registry=repository,
        run_repository=SQLAlchemyModelTrainingRepository(engine),
        provider_name="stub",
    )
    with pytest.raises(ResourceNotFoundError):
        await service.get(
            TrustedTenantContext(
                tenant_id="tenant-b",
                actor_id="reviewer-b",
                permissions=frozenset(),
            ),
            job.job_id,
        )
    leases = await repository.claim_jobs(worker_id="worker-a", limit=1, lease_seconds=30)
    assert len(leases) == 1
    assert leases[0].job.claim_count == 1
    assert leases[0].job.revision == 2

    run_repository = SQLAlchemyModelTrainingRepository(engine)
    await run_repository.save_training_run(
        TrainingRun(
            training_run_id=job.training_run_id,
            tenant_id=job.tenant_id,
            target_type=TrainingTargetType.RERANKER,
            provider=job.provider,
            base_model=job.base_model,
            base_model_version=job.base_model_version,
            candidate_model_version_id="m" * 64,
            candidate_model_version=job.model_version,
            training_dataset_tenant_scope=job.tenant_scope,
            training_dataset_id=job.dataset_id,
            training_dataset_version=job.dataset_version,
            validation_dataset_id=job.dataset_id,
            validation_dataset_version=job.dataset_version,
            evaluation_dataset_id=job.dataset_id,
            evaluation_dataset_version=job.dataset_version,
            schema_version=job.schema_version,
            hyperparameters=(),
            code_version=job.code_version,
            training_artifact_references=(export.manifest_uri,),
            status=TrainingRunStatus.EXPORT_READY,
            created_at=now,
        )
    )
    await repository.advance(
        leases[0],
        status=TrainingJobStatus.QUARANTINED,
        failure_code="training_provider_not_configured",
    )
    old_run = await run_repository.get_training_run(job.tenant_id, job.training_run_id)
    assert old_run is not None
    assert old_run.status is TrainingRunStatus.UNSUPPORTED
