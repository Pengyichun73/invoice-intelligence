"""真实 Suite 完成事实必须同时包含版本化聚合报告产物。"""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.errors import ServiceUnavailableError
from invoice_intelligence.application.services.offline_evaluation import (
    OfflineEvaluationPolicy,
    OfflineEvaluationRequest,
    OfflineEvaluationService,
)
from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationCase,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationRunStatus,
    ExtractionEvaluationOutput,
)
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import (
    SQLAlchemyEvaluationRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import Base

NOW = datetime(2026, 9, 24, tzinfo=UTC)


class _Datasets:
    def __init__(self, dataset):
        self.dataset = dataset

    async def get_dataset(self, tenant_id, dataset_id, dataset_version):
        return self.dataset


class _Runs:
    def __init__(self):
        self.saved = []

    async def get_run(self, tenant_id, evaluation_run_id):
        return None

    async def save(self, run):
        self.saved.append(run)


class _Executor:
    async def evaluate(self, dataset, variant, bindings, suite):
        return (EvaluationCaseObservation(
            case_id="case-1", tenant_id="tenant-a", variant=variant,
            retrieved_examples=(),
            extraction=ExtractionEvaluationOutput(
                actual_value=None, predicted_missing=True, candidate_values=(),
                review_required=False, current_evidence_sufficient=True,
                used_historical_prior_as_value=False,
            ),
        ),)


class _Publisher:
    def __init__(self, references):
        self.references = references

    async def publish(self, run):
        assert run.status is EvaluationRunStatus.COMPLETED
        return self.references


def _service(publisher):
    dataset = EvaluationDataset(
        dataset_id="dataset-1", tenant_id="tenant-a", name="held-out",
        version="v1", schema_version="3.0.0",
        training_document_ids=("training-document",),
        training_template_fingerprints=("training-template",),
        cases=(EvaluationCase(
            case_id="case-1", tenant_id="tenant-a", document_id="held-out-document",
            document_type="invoice", field_path="invoice_number",
            schema_version="3.0.0", vendor_fingerprint=None,
            template_fingerprint="held-out-template", image_quality_bucket="readable",
            evidence_reference=ExampleEvidenceReference(
                document_reference="isolated://held-out-document",
            ),
            expected_examples=(), expected_value=None, expected_is_missing=True,
            expects_candidate=False, expected_review_required=False,
            reviewer_id="reviewer-a", created_at=NOW,
        ),),
        created_at=NOW,
    )
    runs = _Runs()
    service = OfflineEvaluationService(
        dataset_repository=_Datasets(dataset), run_repository=runs,
        variant_executor=_Executor(), artifact_publisher=publisher,
        policy=OfflineEvaluationPolicy(), clock=lambda: NOW,
    )
    request = OfflineEvaluationRequest(
        evaluation_run_id="run-1", tenant_id="tenant-a",
        dataset_id="dataset-1", dataset_version="v1",
        bindings=EvaluationBindings(
            index_version=IndexVersion("index-v1"),
            model_version=ModelVersion("model-v1"),
            prompt_version=PromptVersion("prompt-v1"),
            retrieval_policy_version=RetrievalPolicyVersion("retrieval-v1"),
            threshold_version="threshold-v1",
        ),
    )
    return service, request, runs


@pytest.mark.parametrize("missing_part", ("training_templates", "evaluation_template"))
def test_dataset_requires_both_template_split_fingerprints(missing_part: str) -> None:
    service, _, _ = _service(_Publisher(("isolated://report",)))
    dataset = service._datasets.dataset
    with pytest.raises(ValueError, match="template fingerprint"):
        if missing_part == "training_templates":
            replace(dataset, training_template_fingerprints=())
        else:
            replace(dataset, cases=(replace(dataset.cases[0], template_fingerprint=None),))


@pytest.mark.asyncio
async def test_report_publisher_is_required_before_run_creation() -> None:
    service, request, runs = _service(None)
    with pytest.raises(ServiceUnavailableError):
        await service.evaluate(request)
    assert runs.saved == []


@pytest.mark.asyncio
async def test_missing_report_artifact_fails_without_completed_fact() -> None:
    service, request, runs = _service(_Publisher(()))
    with pytest.raises(ServiceUnavailableError):
        await service.evaluate(request)
    assert [run.status for run in runs.saved] == [
        EvaluationRunStatus.CREATED,
        EvaluationRunStatus.RUNNING,
        EvaluationRunStatus.FAILED,
    ]
    assert runs.saved[-1].promotion_candidates == ()


@pytest.mark.asyncio
async def test_report_artifact_is_bound_to_completed_run() -> None:
    service, request, runs = _service(_Publisher(("isolated://report.json",)))
    completed = await service.evaluate(request)
    assert completed.status is EvaluationRunStatus.COMPLETED
    assert completed.artifact_references == ("isolated://report.json",)
    assert runs.saved[-1] == completed


@pytest.mark.asyncio
async def test_publication_failure_is_persisted_as_failed_run() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    repository = SQLAlchemyEvaluationRepository(engine)
    service, request, _runs = _service(_Publisher(()))
    await repository.add(service._datasets.dataset)
    service._datasets = repository
    service._runs = repository
    with pytest.raises(ServiceUnavailableError):
        await service.evaluate(request)
    persisted = await repository.get_run("tenant-a", "run-1")
    assert persisted is not None
    assert persisted.status is EvaluationRunStatus.FAILED
    assert persisted.artifact_references == ()
    assert persisted.promotion_candidates == ()
    engine.dispose()


@pytest.mark.asyncio
async def test_completed_report_binding_persists_atomically() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    repository = SQLAlchemyEvaluationRepository(engine)
    service, request, _runs = _service(_Publisher(("isolated://report.json",)))
    await repository.add(service._datasets.dataset)
    service._datasets = repository
    service._runs = repository
    completed = await service.evaluate(request)
    persisted = await repository.get_run("tenant-a", "run-1")
    assert persisted == completed
    assert persisted is not None
    assert persisted.status is EvaluationRunStatus.COMPLETED
    assert persisted.artifact_references == ("isolated://report.json",)
    engine.dispose()
