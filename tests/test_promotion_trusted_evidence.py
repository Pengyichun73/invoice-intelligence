"""晋升证据只能来自隔离 PostgreSQL 等价事实，不能来自客户端断言。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invoice_intelligence.api.schemas.promotion import (
    PromotionCandidateCreateRequest,
    PromotionRollbackRequest,
)
from invoice_intelligence.application.errors import ResourceConflictError, ResourceNotFoundError
from invoice_intelligence.application.services.promotion_candidates import (
    PromotionCandidateService,
    PromotionPolicy,
)
from invoice_intelligence.domain.evaluation import (
    CASE_RAG_EVALUATION_VARIANTS,
    EvaluationBindings,
    EvaluationBucket,
    EvaluationBucketDimension,
    EvaluationBucketResult,
    EvaluationCase,
    EvaluationDataset,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationVariant,
    EvaluationVariantResult,
    ExtractionMetric,
    PromotionCandidate,
    RetrievalMetric,
)
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.domain.training import (
    MetricDirection,
    ModelArtifact,
    ModelEvaluation,
    ModelEvaluationStatus,
    ModelLifecycleStage,
    ModelMetricComparison,
    PromotionCandidateStatus,
    TrainingTargetType,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import (
    _dataset_payload,
    _run_payload,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation_jobs import (
    SQLAlchemyEvaluationJobRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_model_training import (
    _artifact_payload,
    _evaluation_payload,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    EvaluationDatasetRow,
    EvaluationJobReportRow,
    EvaluationJobRow,
    EvaluationReportArtifactRow,
    EvaluationRunRow,
    IndexVersionRow,
    ModelArtifactRow,
    ModelEvaluationRow,
    ModelVersionRow,
    PromotionCandidateAuditRow,
    PromotionCandidateRow,
    PromptVersionRow,
    TrainingDatasetVersionRow,
    TrainingRunRow,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_promotion_candidates import (
    SQLAlchemyPromotionCandidateRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_promotion_evidence import (
    SQLAlchemyPromotionEvidenceRepository,
)
from invoice_intelligence.infrastructure.reporting.postgres_evaluation import (
    _artifact_id,
    _reference,
    _report_contents,
)

NOW = datetime(2026, 9, 23, tzinfo=UTC)
PROMPT = "invoice-example-retrieval-v1"
THRESHOLD = "hybrid-thresholds-v1"


def _bucket(*, erroneous_auto_fill: int = 0) -> EvaluationBucketResult:
    return EvaluationBucketResult(
        bucket=EvaluationBucket(EvaluationBucketDimension.OVERALL, "all"),
        retrieval_metrics=(
            RetrievalMetric(
                k=5,
                recall_at_k=0.8,
                hit_rate_at_k=0.8,
                mrr=0.8,
                ndcg_at_k=0.8,
                positive_negative_separation=0.0,
                empty_retrieval_rate=0.0,
                evaluation_case_count=1,
                expected_example_case_count=1,
                expected_example_count=1,
                relevant_hit_count=1,
                empty_retrieval_count=0,
                separation_case_count=1,
            ),
        ),
        extraction_metric=ExtractionMetric(
            field_accuracy=0.9,
            missing_recognition_accuracy=1.0,
            candidate_hit_rate=None,
            erroneous_auto_filled_value_count=erroneous_auto_fill,
            historical_override_violation_count=0,
            review_required_precision=None,
            review_required_recall=None,
            evaluation_case_count=1,
            field_accuracy_denominator=1,
            missing_recognition_correct_count=1,
            candidate_case_count=0,
            candidate_hit_count=0,
            review_true_positive_count=0,
            review_false_positive_count=0,
            review_false_negative_count=0,
        ),
    )


def _seed_bundle(session: Session, number: int, *, hard_failure: bool = False) -> None:
    suffix = str(number)
    artifact_id = f"artifact-{suffix}"
    model_id = f"model-id-{suffix}"
    model_version = f"model-v{suffix}"
    run_id = f"evaluation-{suffix}"
    candidate_id = f"promotion-{suffix}"
    dataset_key = f"eval-dataset-key-{suffix}"
    training_key = f"training-key-{suffix}"
    training_dataset_key = f"training-dataset-key-{suffix}"
    run = EvaluationRun(
        evaluation_run_id=run_id,
        tenant_id="tenant-a",
        dataset_id=f"eval-dataset-{suffix}",
        dataset_version=f"eval-dataset-v{suffix}",
        schema_version="3.0.0",
        bindings=EvaluationBindings(
            index_version=IndexVersion("index-v1"),
            model_version=ModelVersion(model_version),
            prompt_version=PromptVersion(PROMPT),
            retrieval_policy_version=RetrievalPolicyVersion("policy-v1"),
            threshold_version=THRESHOLD,
        ),
        variants=CASE_RAG_EVALUATION_VARIANTS,
        status=EvaluationRunStatus.COMPLETED,
        results=tuple(
            EvaluationVariantResult(
                variant=variant,
                overall=_bucket(erroneous_auto_fill=hard_failure and 1 or 0),
                buckets=(),
            )
            for variant in CASE_RAG_EVALUATION_VARIANTS
        ),
        promotion_candidates=(
            PromotionCandidate(
                candidate_id=candidate_id,
                tenant_id="tenant-a",
                evaluation_run_id=run_id,
                baseline_variant=EvaluationVariant.NO_MEMORY,
                candidate_variant=EvaluationVariant.HYBRID,
                metric_deltas={"field_accuracy": 0.1},
                rationale_codes=("field_accuracy_guard_passed",),
                created_at=NOW,
            ),
        ),
        leakage_check_passed=True,
        report_schema_version="invoice-offline-evaluation-v2",
        artifact_references=(),
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
    )
    run_key = f"eval-run-key-{suffix}"
    report_rows = []
    report_references = []
    for kind, content in _report_contents(run):
        report_id = _artifact_id(run_key, kind)
        digest = sha256(content.encode("utf-8")).hexdigest()
        report_rows.append(EvaluationReportArtifactRow(
            artifact_id=report_id, evaluation_run_key=run_key,
            tenant_id="tenant-a", report_kind=kind,
            report_schema_version=run.report_schema_version,
            content_sha256=digest, content_text=content, created_at=NOW,
        ))
        report_references.append(_reference(report_id, digest))
    run = replace(run, artifact_references=tuple(report_references))
    dataset = EvaluationDataset(
        dataset_id=run.dataset_id,
        tenant_id="tenant-a",
        name="isolated-promotion-evidence",
        version=run.dataset_version,
        schema_version=run.schema_version,
        training_document_ids=(f"training-document-{suffix}",),
        training_template_fingerprints=(f"training-template-{suffix}",),
        cases=(EvaluationCase(
            case_id=f"case-{suffix}", tenant_id="tenant-a",
            document_id=f"held-out-document-{suffix}", document_type="invoice",
            field_path="invoice_number", schema_version=run.schema_version,
            vendor_fingerprint=None, template_fingerprint=f"held-out-template-{suffix}",
            image_quality_bucket="readable",
            evidence_reference=ExampleEvidenceReference(
                document_reference=f"isolated://document-{suffix}",
            ),
            expected_examples=(), expected_value="synthetic", expected_is_missing=False,
            expects_candidate=False, expected_review_required=False,
            reviewer_id="reviewer-a", created_at=NOW,
        ),),
        created_at=NOW,
    )
    artifact = ModelArtifact(
        artifact_id=artifact_id,
        tenant_id="tenant-a",
        training_run_id=f"training-run-{suffix}",
        model_version_id=model_id,
        model_version=model_version,
        target_type=TrainingTargetType.RERANKER,
        provider="isolated-test",
        provider_artifact_reference=f"synthetic://artifact-{suffix}",
        base_model="synthetic-base",
        base_model_version="v1",
        training_dataset_id=f"training-dataset-{suffix}",
        training_dataset_version=f"training-dataset-v{suffix}",
        code_version="test-v1",
        stage=ModelLifecycleStage.REGISTERED,
        created_at=NOW,
    )
    evaluation = ModelEvaluation(
        model_evaluation_id=f"model-evaluation-{suffix}",
        tenant_id="tenant-a",
        artifact_id=artifact_id,
        stage=ModelLifecycleStage.OFFLINE_EVALUATION,
        evaluation_run_id=run_id,
        dataset_id=run.dataset_id,
        dataset_version=run.dataset_version,
        schema_version=run.schema_version,
        baseline_model_version_id="baseline-model",
        candidate_model_version_id=model_id,
        prompt_version=PROMPT,
        threshold_version=THRESHOLD,
        comparisons=(
            ModelMetricComparison(
                metric_name="field_accuracy",
                baseline_value=0.7,
                candidate_value=0.9,
                direction=MetricDirection.HIGHER_IS_BETTER,
                maximum_regression=0.0,
            ),
        ),
        status=ModelEvaluationStatus.PASSED,
        created_at=NOW,
        started_at=NOW,
        completed_at=NOW,
    )
    session.add_all((
        EvaluationDatasetRow(
            dataset_key=dataset_key, tenant_id="tenant-a", dataset_id=run.dataset_id,
            dataset_version=run.dataset_version, schema_version=run.schema_version,
            dataset_json=_dataset_payload(dataset), created_at=NOW,
        ),
        EvaluationRunRow(
            evaluation_run_key=run_key, evaluation_run_id=run_id,
            tenant_id="tenant-a", dataset_key=dataset_key, dataset_id=run.dataset_id,
            dataset_version=run.dataset_version, schema_version=run.schema_version,
            index_version="index-v1", model_version=model_version, prompt_version=PROMPT,
            retrieval_policy_version="policy-v1", threshold_version=THRESHOLD,
            status="completed", run_json=_run_payload(run), created_at=NOW,
            started_at=NOW, completed_at=NOW,
        ),
        EvaluationJobRow(
            job_id=f"suite-job-{suffix}", tenant_id="tenant-a",
            snapshot_id=None, dataset_key=dataset_key, dataset_id=run.dataset_id,
            evidence_class="suite_run", suite=run.suite.value,
            retrieval_policy_version=run.bindings.retrieval_policy_version.value,
            catalog_version=None, admission_policy_version=None,
            field_binding_policy_version=None, evaluation_run_id=run_id,
            dataset_version=run.dataset_version, schema_version=run.schema_version,
            index_version=run.bindings.index_version.value,
            model_version=run.bindings.model_version.value,
            prompt_version=run.bindings.prompt_version.value,
            threshold_version=run.bindings.threshold_version,
            request_sha256="a" * 64, idempotency_key_hash=None,
            status="completed", attempt_count=1, next_attempt_at=None,
            lease_expires_at=None, worker_id=None, lease_token=None,
            failure_code=None, created_at=NOW, updated_at=NOW,
        ),
        EvaluationJobReportRow(
            report_id=f"suite-report-{suffix}", job_id=f"suite-job-{suffix}",
            tenant_id="tenant-a", metrics_json={
                "evidence_class": "suite_run", "evaluation_run_id": run_id,
                "report_schema_version": run.report_schema_version,
                "variant_count": len(run.results),
                "artifact_count": len(run.artifact_references),
            }, created_at=NOW,
        ),
        TrainingDatasetVersionRow(
            dataset_key=training_dataset_key, tenant_scope="tenant-a",
            dataset_id=artifact.training_dataset_id,
            dataset_version=artifact.training_dataset_version,
            source_tenant_ids_json=["tenant-a"], schema_version="3.0.0",
            generation_rule_version="gen-v1", redaction_policy_version="mask-v1",
            split_rule_version="split-v1", split_salt_version="salt-v1",
            created_by="reviewer-a", cross_tenant=False, authorization_id=None,
            status="exported", record_count=1, fingerprint=f"fingerprint-{suffix}",
            dataset_json={}, created_at=NOW, completed_at=NOW,
        ),
        TrainingRunRow(
            training_run_key=training_key,
            training_run_id=artifact.training_run_id,
            tenant_id="tenant-a", training_dataset_key=training_dataset_key,
            target_type="reranker", provider="isolated-test",
            candidate_model_version_id=model_id, candidate_model_version=model_version,
            schema_version="3.0.0", status="succeeded", remote_job_id=None,
            run_json={}, created_at=NOW, completed_at=NOW,
        ),
        ModelVersionRow(
            model_version_id=model_id, tenant_id="tenant-a",
            version=model_version, created_at=NOW,
        ),
        ModelArtifactRow(
            artifact_id=artifact_id, tenant_id="tenant-a", training_run_key=training_key,
            model_version_id=model_id, target_type="reranker", provider="isolated-test",
            stage="registered", is_valid=True, artifact_json=_artifact_payload(artifact),
            created_at=NOW, invalidated_at=None,
        ),
        ModelEvaluationRow(
            model_evaluation_key=f"model-eval-key-{suffix}",
            model_evaluation_id=evaluation.model_evaluation_id,
            tenant_id="tenant-a", artifact_id=artifact_id,
            stage="offline_evaluation", evaluation_run_id=run_id,
            dataset_id=run.dataset_id, dataset_version=run.dataset_version,
            schema_version="3.0.0", baseline_model_version_id="baseline-model",
            candidate_model_version_id=model_id, prompt_version=PROMPT,
            threshold_version=THRESHOLD, status="passed",
            evaluation_json=_evaluation_payload(evaluation), created_at=NOW,
            started_at=NOW, completed_at=NOW,
        ),
    ))
    session.add_all(report_rows)


def _service(*, hard_failure: bool = False) -> tuple[PromotionCandidateService, Engine]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session, session.begin():
        session.add(PromptVersionRow(
            prompt_version_id="prompt-id", tenant_id="tenant-a",
            version=PROMPT, created_at=NOW,
        ))
        session.add(IndexVersionRow(
            index_version_id="index-id", tenant_id="tenant-a", version="index-v1",
            schema_version="3.0.0", dense_model_version_id="model-id-1",
            sparse_model_version_id=None, rerank_model_version_id=None,
            prompt_version_id="prompt-id", is_active=True, is_valid=True,
            invalidated_reason=None, created_at=NOW, activated_at=NOW,
            retired_at=None, invalidated_at=None,
        ))
        _seed_bundle(session, 1, hard_failure=hard_failure)
        _seed_bundle(session, 2)
    repository = SQLAlchemyPromotionCandidateRepository(engine)
    return PromotionCandidateService(
        repository=repository,
        evidence_repository=SQLAlchemyPromotionEvidenceRepository(
            engine,
            active_schema_version="3.0.0",
            active_prompt_version=PROMPT,
            active_threshold_version=THRESHOLD,
        ),
        policy=PromotionPolicy(),
    ), engine


async def _create(service: PromotionCandidateService, number: int = 1):
    return await service.create(
        tenant_id="tenant-a",
        candidate_id=f"promotion-{number}",
        evaluation_run_id=f"evaluation-{number}",
        artifact_id=f"artifact-{number}",
        actor_id="reviewer-a",
        trace_id="trace-test",
    )


async def _active(service: PromotionCandidateService, candidate_id: str) -> None:
    await service.approve(
        tenant_id="tenant-a", candidate_id=candidate_id, expected_revision=1,
        actor_id="reviewer-a", trace_id=None,
        target_status=PromotionCandidateStatus.CANARY,
    )
    await service.approve(
        tenant_id="tenant-a", candidate_id=candidate_id, expected_revision=2,
        actor_id="reviewer-a", trace_id=None,
        target_status=PromotionCandidateStatus.ACTIVE,
    )


@pytest.mark.asyncio
async def test_trusted_create_approval_and_historical_rollback() -> None:
    service, engine = _service()
    first = await _create(service)
    assert first.status is PromotionCandidateStatus.SHADOW
    assert first.artifact_id == "artifact-1"
    assert first.metric_values == {"field_accuracy": 0.9}
    await _active(service, "promotion-1")
    await _create(service, 2)
    await _active(service, "promotion-2")
    with Session(engine) as session:
        old = session.get(PromotionCandidateRow, "promotion-1")
        assert old is not None and old.status == "rollback"
    restored = await service.rollback(
        tenant_id="tenant-a", candidate_id="promotion-2",
        target_candidate_id="promotion-1", expected_revision=3,
        actor_id="reviewer-a", trace_id=None,
    )
    assert restored.candidate_id == "promotion-1"
    assert restored.status is PromotionCandidateStatus.ACTIVE
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(PromotionCandidateRow).where(
            PromotionCandidateRow.status == "active"
        )) == 1
        assert session.scalar(select(func.count()).select_from(PromotionCandidateAuditRow)) == 9


@pytest.mark.asyncio
async def test_missing_foreign_and_invalidated_evidence_fail_closed() -> None:
    service, engine = _service()
    with pytest.raises(ResourceNotFoundError):
        await service.create(
            tenant_id="tenant-b", candidate_id="promotion-1",
            evaluation_run_id="evaluation-1", artifact_id="artifact-1",
            actor_id="reviewer-a", trace_id=None,
        )
    with pytest.raises(ResourceConflictError):
        await service.create(
            tenant_id="tenant-a", candidate_id="forged-candidate",
            evaluation_run_id="evaluation-1", artifact_id="artifact-1",
            actor_id="reviewer-a", trace_id=None,
        )
    await _create(service)
    with Session(engine) as session, session.begin():
        artifact = session.get(ModelArtifactRow, "artifact-1")
        assert artifact is not None
        artifact.is_valid = False
    with pytest.raises(ResourceConflictError):
        await service.approve(
            tenant_id="tenant-a", candidate_id="promotion-1",
            expected_revision=1, actor_id="reviewer-a", trace_id=None,
            target_status=PromotionCandidateStatus.CANARY,
        )


@pytest.mark.asyncio
async def test_hard_failure_overrides_high_metric() -> None:
    service, _ = _service(hard_failure=True)
    rejected = await _create(service)
    assert rejected.status is PromotionCandidateStatus.REJECTED
    assert rejected.hard_failure_code == "ERRONEOUS_AUTO_FILL"
    with pytest.raises(ResourceConflictError):
        await service.approve(
            tenant_id="tenant-a", candidate_id="promotion-1",
            expected_revision=1, actor_id="reviewer-a", trace_id=None,
            target_status=PromotionCandidateStatus.CANARY,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_part", ("report_version", "report_artifact", "dataset", "template_fingerprint")
)
async def test_incomplete_offline_evaluation_evidence_blocks_promotion(
    invalid_part: str,
) -> None:
    service, engine = _service()
    with Session(engine) as session, session.begin():
        run_row = session.get(EvaluationRunRow, "eval-run-key-1")
        dataset_row = session.get(EvaluationDatasetRow, "eval-dataset-key-1")
        assert run_row is not None and dataset_row is not None
        if invalid_part == "dataset":
            dataset_row.dataset_json = {}
        elif invalid_part == "template_fingerprint":
            dataset_payload = dict(dataset_row.dataset_json)
            cases = [dict(item) for item in dataset_payload["cases"]]
            cases[0]["template_fingerprint"] = None
            dataset_payload["cases"] = cases
            dataset_row.dataset_json = dataset_payload
        else:
            payload = dict(run_row.run_json)
            if invalid_part == "report_version":
                payload["report_schema_version"] = "deterministic_stub"
            else:
                payload["artifact_references"] = []
            run_row.run_json = payload
    if invalid_part in {"report_version", "report_artifact"}:
        with pytest.raises(ResourceConflictError, match="Completed Suite Job"):
            await _create(service)
        return
    rejected = await _create(service)
    assert rejected.status is PromotionCandidateStatus.REJECTED
    assert rejected.compatibility_errors == (
        "DATASET_VERSION_INVALID",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_part", ("missing_link", "stale_job", "wrong_version"))
async def test_unconfirmed_or_mismatched_suite_job_cannot_promote(invalid_part: str) -> None:
    service, engine = _service()
    with Session(engine) as session, session.begin():
        job = session.get(EvaluationJobRow, "suite-job-1")
        assert job is not None
        if invalid_part == "missing_link":
            job.evaluation_run_id = None
            job.status = "quarantined"
        elif invalid_part == "stale_job":
            job.status = "running"
            job.evaluation_run_id = None
            job.worker_id = "worker-old"
            job.lease_token = "old-lease"
            job.lease_expires_at = NOW
        else:
            job.index_version = "index-v2"
    with pytest.raises(ResourceConflictError, match="Completed Suite Job"):
        await _create(service)


@pytest.mark.asyncio
async def test_suite_job_completion_is_fenced_and_reloads_run_bindings() -> None:
    _, engine = _service()
    with Session(engine) as session, session.begin():
        job = session.get(EvaluationJobRow, "suite-job-1")
        report = session.get(EvaluationJobReportRow, "suite-report-1")
        assert job is not None and report is not None
        session.delete(report)
        job.status = "running"
        job.evaluation_run_id = None
        job.worker_id = "worker-current"
        job.lease_token = "lease-current"
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    repository = SQLAlchemyEvaluationJobRepository(engine)
    claimed = await repository.get_job("tenant-a", "suite-job-1")
    assert claimed is not None
    with pytest.raises(ResourceConflictError, match="lease"):
        await repository.complete_suite(
            replace(claimed, lease_token="lease-stale"), "evaluation-1"
        )
    await repository.complete_suite(claimed, "evaluation-1")
    completed = await repository.get_job("tenant-a", "suite-job-1")
    assert completed is not None and completed.status.value == "completed"
    assert completed.evaluation_run_id == "evaluation-1"
    assert completed.report is not None
    assert completed.report["evidence_class"] == "suite_run"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ("missing", "checksum", "content", "tenant"))
async def test_report_integrity_blocks_suite_completion_and_promotion(damage: str) -> None:
    service, engine = _service()
    with Session(engine) as session, session.begin():
        report = session.get(EvaluationReportArtifactRow, _artifact_id("eval-run-key-1", "json"))
        job = session.get(EvaluationJobRow, "suite-job-1")
        job_report = session.get(EvaluationJobReportRow, "suite-report-1")
        assert report is not None and job is not None and job_report is not None
        if damage == "missing":
            session.delete(report)
        elif damage == "checksum":
            report.content_sha256 = "0" * 64
        elif damage == "content":
            report.content_text = "{}"
        else:
            report.tenant_id = "tenant-b"
        session.delete(job_report)
        job.status = "running"
        job.evaluation_run_id = None
        job.worker_id = "worker-current"
        job.lease_token = "lease-current"
        job.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    repository = SQLAlchemyEvaluationJobRepository(engine)
    claimed = await repository.get_job("tenant-a", "suite-job-1")
    assert claimed is not None
    with pytest.raises(ResourceConflictError, match="bindings"):
        await repository.complete_suite(claimed, "evaluation-1")
    with pytest.raises(ResourceConflictError, match="Completed Suite Job"):
        await _create(service)


@pytest.mark.asyncio
async def test_legacy_candidate_and_stale_revision_are_blocked() -> None:
    service, engine = _service()
    await _create(service)
    await service.approve(
        tenant_id="tenant-a", candidate_id="promotion-1", expected_revision=1,
        actor_id="reviewer-a", trace_id=None,
        target_status=PromotionCandidateStatus.CANARY,
    )
    with pytest.raises(ResourceConflictError):
        await service.approve(
            tenant_id="tenant-a", candidate_id="promotion-1", expected_revision=1,
            actor_id="reviewer-a", trace_id=None,
            target_status=PromotionCandidateStatus.ACTIVE,
        )
    with Session(engine) as session, session.begin():
        row = session.get(PromotionCandidateRow, "promotion-1")
        assert row is not None
        row.artifact_id = None
        row.model_evaluation_id = None
    with pytest.raises(ResourceConflictError):
        await service.approve(
            tenant_id="tenant-a", candidate_id="promotion-1",
            expected_revision=2, actor_id="reviewer-a", trace_id=None,
            target_status=PromotionCandidateStatus.ACTIVE,
        )


@pytest.mark.asyncio
async def test_rollback_rechecks_historical_target_and_tenant() -> None:
    service, engine = _service()
    await _create(service)
    await _active(service, "promotion-1")
    await _create(service, 2)
    await _active(service, "promotion-2")
    with pytest.raises(ResourceNotFoundError):
        await service.rollback(
            tenant_id="tenant-b", candidate_id="promotion-2",
            target_candidate_id="promotion-1", expected_revision=3,
            actor_id="reviewer-b", trace_id=None,
        )
    with Session(engine) as session, session.begin():
        index = session.get(IndexVersionRow, "index-id")
        assert index is not None
        index.is_valid = False
    with pytest.raises(ResourceConflictError):
        await service.rollback(
            tenant_id="tenant-a", candidate_id="promotion-2",
            target_candidate_id="promotion-1", expected_revision=3,
            actor_id="reviewer-a", trace_id=None,
        )


def test_api_rejects_client_gate_assertions() -> None:
    with pytest.raises(ValidationError):
        PromotionCandidateCreateRequest.model_validate({
            "candidate_id": "promotion-1", "evaluation_run_id": "evaluation-1",
            "artifact_id": "artifact-1", "metric_values": {"field_accuracy": 1.0},
        })
    with pytest.raises(ValidationError):
        PromotionRollbackRequest.model_validate({
            "expected_revision": 1, "target_candidate_id": "promotion-1",
            "validated_target": True,
        })
