"""隔离 Runner 仅接收证据引用，并拒绝错配及不可信响应。"""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256

import httpx
import pytest

from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationCase,
    EvaluationDataset,
    EvaluationSuite,
    EvaluationVariant,
)
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.infrastructure.evaluation.http_runner import (
    IsolatedEvaluationRunnerConfig,
    IsolatedHTTPEvaluationVariantRunner,
)


def _dataset() -> EvaluationDataset:
    return EvaluationDataset(
        dataset_id="dataset-a", tenant_id="tenant-a", name="reviewed",
        version="v1", schema_version="3.0.0",
        training_document_ids=("train-a",),
        training_template_fingerprints=("template-train",),
        cases=(EvaluationCase(
            case_id="case-a", tenant_id="tenant-a", document_id="eval-a",
            document_type="invoice", field_path="invoice_number", schema_version="3.0.0",
            vendor_fingerprint=None, template_fingerprint="template-eval",
            image_quality_bucket="clear",
            evidence_reference=ExampleEvidenceReference(
                document_reference="isolated://eval-a", image_reference="isolated://image-a"
            ),
            expected_examples=(), expected_value="SECRET_LABEL", expected_is_missing=False,
            expects_candidate=False, expected_review_required=False,
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


def _digest(payload: dict[str, object]) -> str:
    return sha256(json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()).hexdigest()


@pytest.mark.asyncio
async def test_http_runner_binds_manifest_without_sending_ground_truth() -> None:
    dataset = _dataset()

    def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert b"SECRET_LABEL" not in request.content
        assert b"reviewer-a" not in request.content
        assert payload["isolation_manifest"]["training_document_ids"] == ["train-a"]
        assert payload["evidence"]["image_reference"] == "isolated://image-a"
        assert request.headers["authorization"] == "Bearer isolated-token"
        return httpx.Response(200, json={
            "request_sha256": _digest(payload),
            "isolation_manifest_sha256": _digest(payload["isolation_manifest"]),
            "observation": {
                "case_id": "case-a", "tenant_id": "tenant-a", "variant": "no_memory",
                "retrieved_examples": [],
                "extraction": {
                    "actual_value": None, "predicted_missing": True,
                    "candidate_values": [], "review_required": False,
                    "current_evidence_sufficient": True,
                    "used_historical_prior_as_value": False,
                },
            },
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        runner = IsolatedHTTPEvaluationVariantRunner(
            EvaluationVariant.NO_MEMORY,
            IsolatedEvaluationRunnerConfig(
                endpoint="https://isolated.example.test/evaluate", bearer_token="isolated-token"
            ), client=client,
        )
        observation = await runner.evaluate_case(
            dataset.cases[0], dataset, _bindings(), EvaluationSuite.CASE_RAG
        )
    assert observation.case_id == "case-a"
    assert observation.variant is EvaluationVariant.NO_MEMORY


@pytest.mark.asyncio
async def test_http_runner_rejects_unbound_or_cross_tenant_response() -> None:
    dataset = _dataset()

    def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json={
            "request_sha256": _digest(payload),
            "isolation_manifest_sha256": "wrong",
            "observation": {},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        runner = IsolatedHTTPEvaluationVariantRunner(
            EvaluationVariant.NO_MEMORY,
            IsolatedEvaluationRunnerConfig(
                endpoint="https://isolated.example.test/evaluate", bearer_token="isolated-token"
            ), client=client,
        )
        with pytest.raises(ValueError, match="binding mismatch"):
            await runner.evaluate_case(
                dataset.cases[0], dataset, _bindings(), EvaluationSuite.CASE_RAG
            )


def test_http_runner_rejects_plaintext_or_embedded_credentials() -> None:
    for endpoint in (
        "http://isolated.example.test/evaluate",
        "https://user:password@isolated.example.test/evaluate",
    ):
        with pytest.raises(ValueError, match="HTTPS endpoint"):
            IsolatedEvaluationRunnerConfig(endpoint=endpoint, bearer_token="isolated-token")


@pytest.mark.asyncio
async def test_http_runner_rejects_nonisolated_evidence_before_network() -> None:
    dataset = _dataset()
    case = replace(
        dataset.cases[0],
        evidence_reference=ExampleEvidenceReference(
            document_reference="https://production.example.test/invoice"
        ),
    )
    dataset = replace(dataset, cases=(case,))
    runner = IsolatedHTTPEvaluationVariantRunner(
        EvaluationVariant.NO_MEMORY,
        IsolatedEvaluationRunnerConfig(
            endpoint="https://isolated.example.test/evaluate", bearer_token="isolated-token"
        ),
    )
    with pytest.raises(ValueError, match="isolated reference"):
        await runner.evaluate_case(case, dataset, _bindings(), EvaluationSuite.CASE_RAG)
