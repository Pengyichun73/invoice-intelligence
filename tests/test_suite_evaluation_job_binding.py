"""真实 Suite Job 只绑定冻结审核数据集，不回退到诊断 Stub。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import (
    BadRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.services.evaluation_execution import (
    ALL_EVALUATION_VARIANTS,
    ConfiguredEvaluationVariantExecutor,
)
from invoice_intelligence.application.services.evaluation_jobs import (
    CreateSuiteEvaluationJobCommand,
    EvaluationJobService,
)
from invoice_intelligence.application.services.offline_evaluation import (
    OfflineEvaluationPolicy,
    OfflineEvaluationService,
)
from invoice_intelligence.domain.evaluation import (
    EvaluationCase,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationSuite,
    EvaluationVariant,
    ExtractionEvaluationOutput,
    required_variants_for_suite,
)
from invoice_intelligence.domain.examples import ExampleEvidenceReference
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import (
    SQLAlchemyEvaluationRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation_jobs import (
    SQLAlchemyEvaluationJobRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    EvaluationDatasetRow,
    EvaluationJobReportRow,
    EvaluationJobRow,
    EvaluationReportArtifactRow,
    EvaluationRunRow,
    EvaluationSnapshotRow,
)
from invoice_intelligence.infrastructure.reporting.postgres_evaluation import (
    PostgreSQLEvaluationArtifactPublisher,
)

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _context(tenant_id: str) -> TrustedTenantContext:
    return TrustedTenantContext(tenant_id=tenant_id, actor_id="reviewer-a", permissions=frozenset())


def _command() -> CreateSuiteEvaluationJobCommand:
    return CreateSuiteEvaluationJobCommand(
        dataset_id="dataset-a", dataset_version="v1", suite=EvaluationSuite.CASE_RAG,
        index_version="index-v1", model_version="model-v1", prompt_version="prompt-v1",
        retrieval_policy_version="retrieval-v1", threshold_version="threshold-v1",
    )


@pytest.mark.parametrize("timeout", (0, -1, float("nan"), float("inf")))
def test_suite_timeout_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        EvaluationJobService(object(), suite_run_timeout_seconds=timeout)


class _Runner:
    def __init__(self, variant: EvaluationVariant, delay: float = 0) -> None:
        self.variant = variant
        self.delay = delay

    async def evaluate_case(
        self, case: EvaluationCase, dataset: EvaluationDataset,
        bindings: object, suite: EvaluationSuite,
    ) -> EvaluationCaseObservation:
        if self.delay:
            await asyncio.sleep(self.delay)
        return EvaluationCaseObservation(
            case_id=case.case_id, tenant_id=case.tenant_id, variant=self.variant,
            retrieved_examples=(),
            extraction=ExtractionEvaluationOutput(
                actual_value=None, predicted_missing=True, candidate_values=(),
                review_required=False, current_evidence_sufficient=True,
                used_historical_prior_as_value=False,
            ),
        )


async def _service(
    *, with_runner: bool = False, delay: float = 0,
    publisher_available: bool = True, run_timeout: float = 3600,
) -> EvaluationJobService:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[
        EvaluationDatasetRow.__table__, EvaluationSnapshotRow.__table__,
        EvaluationJobRow.__table__, EvaluationJobReportRow.__table__,
        EvaluationRunRow.__table__, EvaluationReportArtifactRow.__table__,
    ])
    datasets = SQLAlchemyEvaluationRepository(engine)
    await datasets.add(EvaluationDataset(
        dataset_id="dataset-a", tenant_id="tenant-a", name="held-out",
        version="v1", schema_version="3.0.0",
        training_document_ids=("training-document",),
        training_template_fingerprints=("training-template",),
        cases=(EvaluationCase(
            case_id="case-a", tenant_id="tenant-a", document_id="held-out-document",
            document_type="invoice", field_path="invoice_number", schema_version="3.0.0",
            vendor_fingerprint=None, template_fingerprint="held-out-template",
            image_quality_bucket="readable",
            evidence_reference=ExampleEvidenceReference(
                document_reference="isolated://held-out-document",
            ),
            expected_examples=(), expected_value=None, expected_is_missing=True,
            expects_candidate=False, expected_review_required=False,
            reviewer_id="reviewer-a", created_at=NOW,
        ),),
        created_at=NOW,
    ))
    suite_evaluation = None
    if with_runner:
        suite_evaluation = OfflineEvaluationService(
            dataset_repository=datasets, run_repository=datasets,
            variant_executor=ConfiguredEvaluationVariantExecutor(
                {variant: _Runner(variant, delay) for variant in ALL_EVALUATION_VARIANTS},
                maximum_concurrency=2,
            ),
            artifact_publisher=(
                PostgreSQLEvaluationArtifactPublisher(engine) if publisher_available else None
            ),
            policy=OfflineEvaluationPolicy(),
        )
    return EvaluationJobService(
        SQLAlchemyEvaluationJobRepository(engine), dataset_repository=datasets,
        suite_evaluation=suite_evaluation,
        suite_run_timeout_seconds=run_timeout,
    )


@pytest.mark.asyncio
async def test_suite_job_binding_is_tenant_scoped_and_idempotent() -> None:
    service = await _service()
    job = await service.create_suite_job(_context("tenant-a"), _command(), idempotency_key="key-a")
    assert job.evidence_class == "suite_run"
    assert job.snapshot_id is None
    assert job.dataset_id == "dataset-a"
    assert job.suite is EvaluationSuite.CASE_RAG
    assert job.retrieval_policy_version == "retrieval-v1"
    replay = await service.create_suite_job(
        _context("tenant-a"), _command(), idempotency_key="key-a"
    )
    assert replay.job_id == job.job_id
    with pytest.raises(ResourceConflictError):
        await service.create_suite_job(
            _context("tenant-a"), replace(_command(), index_version="index-v2"),
            idempotency_key="key-a",
        )
    with pytest.raises(ResourceNotFoundError):
        await service.get_job(_context("tenant-b"), job.job_id)
    with pytest.raises(ResourceNotFoundError):
        await service.create_suite_job(_context("tenant-b"), _command(), idempotency_key="key-b")


@pytest.mark.asyncio
async def test_unconfigured_suite_worker_quarantines_without_stub_report() -> None:
    service = await _service()
    job = await service.create_suite_job(_context("tenant-a"), _command(), idempotency_key="key-a")
    await service.execute_one("worker-a")
    result = await service.get_job(_context("tenant-a"), job.job_id)
    assert result.status.value == "quarantined"
    assert result.failure_code == "evaluation.runner_unavailable"
    assert result.report is None


@pytest.mark.asyncio
async def test_configured_suite_job_runs_every_variant_and_confirms_report() -> None:
    service = await _service(with_runner=True)
    job = await service.create_suite_job(_context("tenant-a"), _command(), idempotency_key="key-a")
    await service.execute_one("worker-a", lease_seconds=30)
    completed = await service.get_job(_context("tenant-a"), job.job_id)
    assert completed.status.value == "completed"
    assert completed.evaluation_run_id is not None
    assert completed.report is not None
    assert completed.report["variant_count"] == len(
        required_variants_for_suite(EvaluationSuite.CASE_RAG)
    )
    assert completed.report["report_schema_version"] == "invoice-offline-evaluation-v2"
    assert await service.execute_one("worker-b") is None


@pytest.mark.asyncio
async def test_long_suite_run_renews_lease_before_confirming_report() -> None:
    service = await _service(with_runner=True, delay=0.25)
    job = await service.create_suite_job(_context("tenant-a"), _command(), idempotency_key="key-a")
    await service.execute_one("worker-a", lease_seconds=1)
    completed = await service.get_job(_context("tenant-a"), job.job_id)
    assert completed.status.value == "completed"
    assert completed.evaluation_run_id is not None


@pytest.mark.asyncio
async def test_missing_suite_report_never_confirms_job() -> None:
    service = await _service(with_runner=True, publisher_available=False)
    job = await service.create_suite_job(_context("tenant-a"), _command(), idempotency_key="key-a")
    await service.execute_one("worker-a")
    failed = await service.get_job(_context("tenant-a"), job.job_id)
    assert failed.status.value == "failed"
    assert failed.evaluation_run_id is None
    assert failed.report is None


@pytest.mark.asyncio
async def test_suite_runner_timeout_fails_without_confirmed_run() -> None:
    service = await _service(with_runner=True, delay=0.2, run_timeout=0.05)
    job = await service.create_suite_job(_context("tenant-a"), _command(), idempotency_key="key-a")
    await service.execute_one("worker-a", lease_seconds=1)
    failed = await service.get_job(_context("tenant-a"), job.job_id)
    assert failed.status.value == "failed"
    assert failed.failure_code == "evaluation.runner_timeout"
    assert failed.evaluation_run_id is None
    assert failed.report is None


@pytest.mark.asyncio
async def test_trusted_memory_suite_requires_ground_truth_and_policy_versions() -> None:
    service = await _service()
    with pytest.raises(BadRequestError, match="ground truth"):
        await service.create_suite_job(
            _context("tenant-a"),
            replace(
                _command(), suite=EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING,
                catalog_version="catalog-v1", admission_policy_version="admission-v1",
                field_binding_policy_version="binding-v1",
            ),
            idempotency_key="key-trusted",
        )
