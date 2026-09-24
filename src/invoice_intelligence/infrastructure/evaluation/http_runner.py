"""只读取隔离证据引用的 Suite 变体 HTTP Runner。"""

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationCase,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationRetrievedExample,
    EvaluationVariant,
    ExtractionEvaluationOutput,
    FieldBindingEvaluationOutput,
    MemoryAdmissionEvaluationOutput,
)
from invoice_intelligence.domain.json_types import JsonValue


class _ExtractionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actual_value: object
    predicted_missing: bool
    candidate_values: list[object]
    review_required: bool
    current_evidence_sufficient: bool
    used_historical_prior_as_value: bool


class _ObservationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    tenant_id: str
    variant: EvaluationVariant
    retrieved_examples: list[dict[str, object]]
    extraction: _ExtractionResponse
    memory_admission: dict[str, object] | None = None
    field_binding: dict[str, object] | None = None


class _RunnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_sha256: str
    isolation_manifest_sha256: str
    observation: dict[str, object]


_RETRIEVED_EXAMPLE = TypeAdapter(EvaluationRetrievedExample)
_ADMISSION = TypeAdapter(MemoryAdmissionEvaluationOutput)
_BINDING = TypeAdapter(FieldBindingEvaluationOutput)


@dataclass(frozen=True, slots=True)
class IsolatedEvaluationRunnerConfig:
    endpoint: str
    bearer_token: str
    timeout_seconds: float = 30.0
    max_retries: int = 2
    max_response_bytes: int = 262_144

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Isolated evaluation Runner requires a plain HTTPS endpoint")
        if not self.bearer_token.strip():
            raise ValueError("Isolated evaluation Runner requires a bearer token")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 300:
            raise ValueError("Isolated evaluation Runner timeout is invalid")
        if not 0 <= self.max_retries <= 10 or self.max_response_bytes <= 0:
            raise ValueError("Isolated evaluation Runner limits are invalid")


class IsolatedHTTPEvaluationVariantRunner:
    """发送无标签真值的引用清单，校验远端绑定后返回 Domain Observation。"""

    def __init__(
        self,
        variant: EvaluationVariant,
        config: IsolatedEvaluationRunnerConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._variant = variant
        self._config = config
        self._client = client

    async def evaluate_case(
        self,
        case: EvaluationCase,
        dataset: EvaluationDataset,
        bindings: EvaluationBindings,
    ) -> EvaluationCaseObservation:
        if case.tenant_id != dataset.tenant_id or case not in dataset.cases:
            raise ValueError("Evaluation case is outside the frozen dataset")
        manifest = {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.version,
            "tenant_id": dataset.tenant_id,
            "training_document_ids": sorted(dataset.training_document_ids),
            "evaluation_document_ids": list(dataset.evaluation_document_ids),
            "training_template_fingerprints": sorted(dataset.training_template_fingerprints),
            "evaluation_template_fingerprints": sorted(
                {item.template_fingerprint for item in dataset.cases if item.template_fingerprint}
            ),
        }
        manifest_sha256 = _digest(manifest)
        request = {
            "contract_version": "isolated-evaluation-runner-v1",
            "variant": self._variant.value,
            "case_id": case.case_id,
            "tenant_id": case.tenant_id,
            "document_id": case.document_id,
            "document_type": case.document_type,
            "field_path": case.field_path,
            "schema_version": case.schema_version,
            "evidence": {
                "document_reference": case.evidence_reference.document_reference,
                "image_reference": case.evidence_reference.image_reference,
                "page_number": case.evidence_reference.page_number,
            },
            "versions": {
                "index": bindings.index_version.value,
                "model": bindings.model_version.value,
                "prompt": bindings.prompt_version.value,
                "retrieval_policy": bindings.retrieval_policy_version.value,
                "threshold": bindings.threshold_version,
                "catalog": bindings.catalog_version,
                "admission_policy": bindings.admission_policy_version,
                "field_binding_policy": bindings.field_binding_policy_version,
            },
            "isolation_manifest": manifest,
            "isolation_manifest_sha256": manifest_sha256,
        }
        request_sha256 = _digest(request)
        headers = {"Authorization": f"Bearer {self._config.bearer_token}"}
        for attempt in range(self._config.max_retries + 1):
            try:
                if self._client is None:
                    async with httpx.AsyncClient(timeout=self._config.timeout_seconds) as client:
                        response = await client.post(
                            self._config.endpoint, json=request, headers=headers
                        )
                else:
                    response = await self._client.post(
                        self._config.endpoint, json=request, headers=headers,
                        timeout=self._config.timeout_seconds,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == self._config.max_retries:
                    raise RuntimeError("Isolated evaluation Runner transport failed") from exc
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt == self._config.max_retries:
                    raise RuntimeError("Isolated evaluation Runner temporarily unavailable")
                await asyncio.sleep(min(0.25 * 2**attempt, 2.0))
                continue
            if response.status_code != 200:
                raise ValueError("Isolated evaluation Runner rejected the request")
            if len(response.content) > self._config.max_response_bytes:
                raise ValueError("Isolated evaluation Runner response is too large")
            try:
                payload = _RunnerResponse.model_validate_json(response.content)
                if (
                    payload.request_sha256 != request_sha256
                    or payload.isolation_manifest_sha256 != manifest_sha256
                ):
                    raise ValueError("Isolated evaluation Runner binding mismatch")
                raw = _ObservationResponse.model_validate(payload.observation)
                extraction = raw.extraction
                observation = EvaluationCaseObservation(
                    case_id=raw.case_id,
                    tenant_id=raw.tenant_id,
                    variant=raw.variant,
                    retrieved_examples=tuple(
                        _RETRIEVED_EXAMPLE.validate_python(item)
                        for item in raw.retrieved_examples
                    ),
                    extraction=ExtractionEvaluationOutput(
                        actual_value=cast(JsonValue, extraction.actual_value),
                        predicted_missing=extraction.predicted_missing,
                        candidate_values=cast(tuple[JsonValue, ...], tuple(extraction.candidate_values)),
                        review_required=extraction.review_required,
                        current_evidence_sufficient=extraction.current_evidence_sufficient,
                        used_historical_prior_as_value=(
                            extraction.used_historical_prior_as_value
                        ),
                    ),
                    memory_admission=(
                        _ADMISSION.validate_python(raw.memory_admission)
                        if raw.memory_admission is not None else None
                    ),
                    field_binding=(
                        _BINDING.validate_python(raw.field_binding)
                        if raw.field_binding is not None else None
                    ),
                )
            except ValidationError as exc:
                raise ValueError("Isolated evaluation Runner response is invalid") from exc
            if (
                observation.case_id != case.case_id
                or observation.tenant_id != case.tenant_id
                or observation.variant is not self._variant
            ):
                raise ValueError("Isolated evaluation Runner observation identity mismatch")
            return observation
        raise RuntimeError("Isolated evaluation Runner attempts exhausted")


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
