"""独立只读评估服务与 Worker Adapter 的隔离契约。"""

from collections.abc import Mapping
from datetime import UTC, datetime

import httpx
import pytest

from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationCase,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationSuite,
    EvaluationVariant,
    ExtractionEvaluationOutput,
    required_variants_for_suite,
)
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.evaluation_runner.entrypoint import create_app
from invoice_intelligence.evaluation_runner.service import (
    EvaluationRequest,
    IsolatedEvaluationService,
    ReadOnlyEvidenceSnapshot,
    create_isolated_evaluation_app,
)
from invoice_intelligence.infrastructure.evaluation.http_runner import (
    IsolatedEvaluationRunnerConfig,
    IsolatedHTTPEvaluationVariantRunner,
)


class _Evidence(ReadOnlyEvidenceSnapshot):
    def __init__(self) -> None:
        self.reads: list[tuple[str, str]] = []

    def read(self, tenant_id: str, reference: str) -> bytes:
        self.reads.append((tenant_id, reference))
        if tenant_id != "tenant-a" or reference not in {
            "isolated://eval-a", "isolated://image-a"
        }:
            raise ValueError("Evidence unavailable")
        return b"deidentified-evidence"


class _Engine:
    async def evaluate(
        self, request: EvaluationRequest, document: bytes, image: bytes | None
    ) -> EvaluationCaseObservation:
        assert document == image == b"deidentified-evidence"
        return EvaluationCaseObservation(
            case_id=request.case_id,
            tenant_id=request.tenant_id,
            variant=request.variant,
            retrieved_examples=(),
            extraction=ExtractionEvaluationOutput(
                actual_value=None,
                predicted_missing=True,
                candidate_values=(),
                review_required=True,
                current_evidence_sufficient=False,
                used_historical_prior_as_value=False,
            ),
        )


def _dataset() -> EvaluationDataset:
    return EvaluationDataset(
        dataset_id="dataset-a", tenant_id="tenant-a", name="deidentified",
        version="v1", schema_version="3.0.0",
        training_document_ids=("train-a",),
        training_template_fingerprints=("template-train",),
        cases=(EvaluationCase(
            case_id="case-a", tenant_id="tenant-a", document_id="eval-a",
            document_type="invoice", field_path="invoice_number", schema_version="3.0.0",
            vendor_fingerprint=None, template_fingerprint="template-eval",
            image_quality_bucket="clear",
            evidence_reference=ExampleEvidenceReference(
                document_reference="isolated://eval-a",
                image_reference="isolated://image-a",
            ),
            expected_examples=(), expected_value=None, expected_is_missing=True,
            expects_candidate=False, expected_review_required=True,
            reviewer_id="reviewer-a", created_at=datetime(2026, 9, 24, tzinfo=UTC),
        ),),
        created_at=datetime(2026, 9, 24, tzinfo=UTC),
    )


def _bindings() -> EvaluationBindings:
    return EvaluationBindings(
        index_version=IndexVersion("i1"), model_version=ModelVersion("m1"),
        prompt_version=PromptVersion("p1"),
        retrieval_policy_version=RetrievalPolicyVersion("r1"),
        threshold_version="t1",
    )


@pytest.mark.asyncio
async def test_all_variants_round_trip_through_isolated_service() -> None:
    evidence = _Evidence()
    engines: Mapping[EvaluationVariant, _Engine] = {
        variant: _Engine() for variant in EvaluationVariant
    }
    service = IsolatedEvaluationService("token-a", evidence, engines)
    app = create_isolated_evaluation_app(service)
    dataset = _dataset()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://isolated.example.test"
    ) as client:
        for suite in EvaluationSuite:
            for variant in required_variants_for_suite(suite):
                runner = IsolatedHTTPEvaluationVariantRunner(
                    variant,
                    IsolatedEvaluationRunnerConfig(
                        endpoint="https://isolated.example.test/evaluate",
                        bearer_token="token-a", max_retries=0,
                    ),
                    client=client,
                )
                observation = await runner.evaluate_case(
                    dataset.cases[0], dataset, _bindings(), suite
                )
                assert observation.variant is variant
                assert observation.extraction.review_required is True
        rejected = await client.post(
            "/evaluate", json={}, headers={"Authorization": "Bearer wrong"}
        )
    assert rejected.status_code == 401
    assert len(evidence.reads) == 2 * sum(
        len(required_variants_for_suite(suite)) for suite in EvaluationSuite
    )


def test_missing_variant_engine_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="every evaluation variant"):
        IsolatedEvaluationService("token-a", _Evidence(), {})


def test_missing_isolated_service_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EVALUATION_SERVICE_TOKEN_FILE", raising=False)
    with pytest.raises(ValueError, match="EVALUATION_SERVICE_TOKEN_FILE"):
        create_app()
