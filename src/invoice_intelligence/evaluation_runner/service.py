"""只读证据快照驱动的独立评估 HTTP 服务。"""

import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from invoice_intelligence.domain.evaluation import (
    EvaluationCaseObservation,
    EvaluationSuite,
    EvaluationVariant,
    required_variants_for_suite,
)
from invoice_intelligence.infrastructure.evaluation.http_runner import _digest

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EvidenceRequest(_StrictModel):
    document_reference: str
    image_reference: str | None
    page_number: int | None


class VersionRequest(_StrictModel):
    index: str
    model: str
    prompt: str
    retrieval_policy: str
    threshold: str
    catalog: str | None
    admission_policy: str | None
    field_binding_policy: str | None


class ManifestCase(_StrictModel):
    case_id: str
    document_id: str
    template_fingerprint: str


class IsolationManifest(_StrictModel):
    dataset_id: str
    dataset_version: str
    tenant_id: str
    training_document_ids: list[str]
    evaluation_document_ids: list[str]
    training_template_fingerprints: list[str]
    evaluation_template_fingerprints: list[str]
    evaluation_cases: list[ManifestCase]


class EvaluationRequest(_StrictModel):
    contract_version: str
    suite: EvaluationSuite
    variant: EvaluationVariant
    case_id: str
    tenant_id: str
    document_id: str
    document_type: str
    field_path: str
    schema_version: str
    template_fingerprint: str
    evidence: EvidenceRequest
    versions: VersionRequest
    isolation_manifest: IsolationManifest
    isolation_manifest_sha256: str


class IsolatedVariantEngine(Protocol):
    """引擎只能消费隔离证据及版本绑定；不得读取 Ground Truth。"""

    async def evaluate(
        self,
        request: EvaluationRequest,
        document: bytes,
        image: bytes | None,
    ) -> EvaluationCaseObservation: ...


class ReadOnlyEvidenceSnapshot:
    """从租户目录读取已登记 SHA-256 的脱敏证据。"""

    def __init__(
        self,
        root: Path,
        checksums: Mapping[str, Mapping[str, str]],
        *,
        max_evidence_bytes: int = 20_971_520,
    ) -> None:
        self._root = root.resolve(strict=True)
        if not self._root.is_dir() or max_evidence_bytes <= 0:
            raise ValueError("Invalid isolated evidence root or size limit")
        self._checksums = {tenant: dict(entries) for tenant, entries in checksums.items()}
        self._max_evidence_bytes = max_evidence_bytes

    def read(self, tenant_id: str, reference: str) -> bytes:
        if not _KEY.fullmatch(tenant_id):
            raise ValueError("Invalid isolated tenant")
        parsed = urlsplit(reference)
        if (
            parsed.scheme != "isolated"
            or not _KEY.fullmatch(parsed.netloc)
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
        ):
            raise ValueError("Invalid isolated evidence reference")
        expected = self._checksums.get(tenant_id, {}).get(reference)
        if expected is None or not _SHA256.fullmatch(expected):
            raise ValueError("Isolated evidence is not registered")
        tenant_root = (self._root / tenant_id).resolve(strict=True)
        if tenant_root.parent != self._root or (self._root / tenant_id).is_symlink():
            raise ValueError("Isolated tenant directory escaped evidence root")
        path = tenant_root / parsed.netloc
        if path.is_symlink() or path.resolve(strict=True).parent != tenant_root:
            raise ValueError("Isolated evidence escaped tenant root")
        if not path.is_file() or path.stat().st_size > self._max_evidence_bytes:
            raise ValueError("Isolated evidence is missing or oversized")
        with path.open("rb") as stream:
            content = stream.read(self._max_evidence_bytes + 1)
        if len(content) > self._max_evidence_bytes or sha256(content).hexdigest() != expected:
            raise ValueError("Isolated evidence checksum mismatch")
        return content


class IsolatedEvaluationService:
    def __init__(
        self,
        token: str,
        evidence: ReadOnlyEvidenceSnapshot,
        engines: Mapping[EvaluationVariant, IsolatedVariantEngine],
    ) -> None:
        if not token or set(engines) != set(EvaluationVariant):
            raise ValueError("Token and every evaluation variant engine are required")
        self._token = token
        self._evidence = evidence
        self._engines = engines

    async def evaluate(
        self, authorization: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        if (
            authorization is None
            or not secrets.compare_digest(authorization, f"Bearer {self._token}")
        ):
            raise HTTPException(status_code=401, detail="Evaluation authentication failed")
        try:
            request = EvaluationRequest.model_validate_json(json.dumps(payload))
            manifest = request.isolation_manifest
            manifest_data = manifest.model_dump(mode="json")
            if (
                request.contract_version != "isolated-evaluation-runner-v1"
                or request.variant not in required_variants_for_suite(request.suite)
                or request.isolation_manifest_sha256 != _digest(manifest_data)
                or manifest.tenant_id != request.tenant_id
                or not manifest.dataset_id.strip()
                or not manifest.dataset_version.strip()
                or not request.schema_version.strip()
                or any(
                    not value
                    for value in request.versions.model_dump().values()
                    if value is not None
                )
                or not manifest.training_document_ids
                or not manifest.training_template_fingerprints
                or len(manifest.evaluation_cases) == 0
                or len(manifest.evaluation_cases)
                != len({case.case_id for case in manifest.evaluation_cases})
                or set(manifest.training_document_ids) & set(manifest.evaluation_document_ids)
                or set(manifest.training_template_fingerprints)
                & set(manifest.evaluation_template_fingerprints)
                or sorted({case.document_id for case in manifest.evaluation_cases})
                != manifest.evaluation_document_ids
                or sorted({case.template_fingerprint for case in manifest.evaluation_cases})
                != manifest.evaluation_template_fingerprints
                or not any(
                    case.case_id == request.case_id
                    and case.document_id == request.document_id
                    and case.template_fingerprint == request.template_fingerprint
                    for case in manifest.evaluation_cases
                )
            ):
                raise ValueError("Invalid frozen evaluation manifest")
            document = self._evidence.read(request.tenant_id, request.evidence.document_reference)
            image = (
                self._evidence.read(request.tenant_id, request.evidence.image_reference)
                if request.evidence.image_reference is not None else None
            )
        except (ValueError, OSError, ValidationError) as exc:
            raise HTTPException(
                status_code=422, detail="Invalid isolated evaluation evidence"
            ) from exc
        observation = await self._engines[request.variant].evaluate(request, document, image)
        if (
            observation.case_id != request.case_id
            or observation.tenant_id != request.tenant_id
            or observation.variant is not request.variant
        ):
            raise HTTPException(status_code=502, detail="Evaluation observation identity mismatch")
        return {
            "request_sha256": _digest(payload),
            "isolation_manifest_sha256": request.isolation_manifest_sha256,
            "observation": asdict(observation),
        }


def create_isolated_evaluation_app(
    service: IsolatedEvaluationService,
    *,
    max_request_bytes: int = 262_144,
) -> FastAPI:
    if max_request_bytes <= 0:
        raise ValueError("Invalid evaluation request size limit")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/evaluate")
    async def evaluate(request: Request) -> dict[str, object]:
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > max_request_bytes:
                raise HTTPException(status_code=413, detail="Evaluation request is too large")
            body.extend(chunk)
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Request must be an object")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid evaluation request") from exc
        return await service.evaluate(request.headers.get("authorization"), payload)

    return app
