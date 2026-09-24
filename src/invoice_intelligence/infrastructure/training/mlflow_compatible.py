"""Configurable REST adapter for MLflow-compatible training control planes."""

from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from invoice_intelligence.application.errors import (
    TrainingProviderPermanentError,
    TrainingProviderUnavailableError,
)
from invoice_intelligence.domain.training_registry import (
    TrainingProviderJobStatus,
    TrainingProviderRequest,
    TrainingProviderSnapshot,
)


class _RemoteJobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: TrainingProviderJobStatus
    updated_at: str
    artifact_uri: str | None = None
    artifact_checksum_sha256: str | None = None
    failure_code: str | None = None


class MLflowCompatibleTrainingProvider:
    """Call explicitly configured endpoints; no MLflow service is required locally."""

    def __init__(
        self,
        *,
        base_url: str,
        submit_path: str,
        job_path_template: str,
        cancel_path_template: str,
        bearer_token: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.startswith("https://"):
            raise ValueError("Remote training base_url must use HTTPS")
        if not bearer_token.strip():
            raise ValueError("Remote training bearer token is required")
        for value in (submit_path, job_path_template, cancel_path_template):
            if not value.startswith("/"):
                raise ValueError("Remote training paths must be absolute URL paths")
        self._base_url = base_url.rstrip("/")
        self._submit_path = submit_path
        self._job_path_template = job_path_template
        self._cancel_path_template = cancel_path_template
        self._headers = {"Authorization": f"Bearer {bearer_token}"}
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
        )
        self._owns_client = client is None

    async def submit(
        self, request: TrainingProviderRequest, *, operation_id: str
    ) -> TrainingProviderSnapshot:
        return await self._request(
            "POST",
            self._submit_path,
            operation_id=operation_id,
            payload={
                "job_id": request.job_id,
                "target_type": request.target_type,
                "base_model": request.base_model,
                "base_model_version": request.base_model_version,
                "dataset_manifest_uri": request.dataset_manifest_uri,
                "dataset_manifest_checksum_sha256": (
                    request.dataset_manifest_checksum_sha256
                ),
                "dataset_version": request.dataset_version,
                "schema_version": request.schema_version,
                "index_version": request.index_version,
                "model_version": request.model_version,
                "prompt_version": request.prompt_version,
                "code_version": request.code_version,
            },
        )

    async def get_job(self, provider_job_id: str) -> TrainingProviderSnapshot:
        return await self._request(
            "GET", self._job_path_template.format(job_id=provider_job_id)
        )

    async def cancel(
        self, provider_job_id: str, *, operation_id: str
    ) -> TrainingProviderSnapshot:
        return await self._request(
            "POST",
            self._cancel_path_template.format(job_id=provider_job_id),
            operation_id=operation_id,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TrainingProviderSnapshot:
        headers = dict(self._headers)
        if operation_id is not None:
            headers["Idempotency-Key"] = operation_id
        try:
            response = await self._client.request(
                method, f"{self._base_url}{path}", headers=headers, json=payload
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise TrainingProviderUnavailableError("training_provider_unavailable") from exc
        if response.status_code in {408, 429} or response.status_code >= 500:
            raise TrainingProviderUnavailableError("training_provider_unavailable")
        if response.status_code >= 400:
            raise TrainingProviderPermanentError("training_provider_request_rejected")
        try:
            parsed = _RemoteJobResponse.model_validate(response.json())
            return TrainingProviderSnapshot(
                provider_job_id=parsed.job_id,
                status=parsed.status,
                updated_at=__import__("datetime").datetime.fromisoformat(parsed.updated_at),
                artifact_uri=parsed.artifact_uri,
                artifact_checksum_sha256=parsed.artifact_checksum_sha256,
                failure_code=parsed.failure_code,
            )
        except (TypeError, ValueError) as exc:
            raise TrainingProviderPermanentError("training_provider_contract_invalid") from exc
