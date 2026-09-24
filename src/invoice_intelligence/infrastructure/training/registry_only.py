"""Registry-only fallbacks when no documented remote training control plane is enabled."""

from collections.abc import Callable
from datetime import UTC, datetime

from invoice_intelligence.application.errors import TrainingDataError
from invoice_intelligence.domain.training import (
    ModelArtifact,
    ModelDeployment,
    ModelLifecycleStage,
    ProviderTrainingCapabilities,
    RemoteTrainingJob,
    TrainingDatasetVersion,
    TrainingRun,
)


class RegistryOnlyTrainingProvider:
    """Declare no trainable targets instead of fabricating provider capabilities."""

    def __init__(
        self,
        *,
        provider_name: str = "registry-only",
        capability_version: str = "registry-only-v1",
        documentation_reference: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not provider_name.strip() or not capability_version.strip():
            raise ValueError("Registry-only provider identity must not be empty")
        self._provider_name = provider_name
        self._capability_version = capability_version
        self._documentation_reference = documentation_reference
        self._clock = clock or (lambda: datetime.now(UTC))

    async def capabilities(self) -> ProviderTrainingCapabilities:
        return ProviderTrainingCapabilities(
            provider=self._provider_name,
            capability_version=self._capability_version,
            supported_targets=(),
            supports_submission=False,
            discovered_at=self._now(),
            documentation_reference=self._documentation_reference,
        )

    async def submit(
        self,
        run: TrainingRun,
        dataset: TrainingDatasetVersion,
        *,
        idempotency_key: str,
    ) -> RemoteTrainingJob:
        del run, dataset, idempotency_key
        raise TrainingDataError("Remote training is not configured for this provider")

    async def get_job(self, provider_job_id: str) -> RemoteTrainingJob:
        del provider_job_id
        raise TrainingDataError("Registry-only provider has no remote training jobs")

    async def cancel(
        self,
        provider_job_id: str,
        *,
        idempotency_key: str,
    ) -> RemoteTrainingJob:
        del provider_job_id, idempotency_key
        raise TrainingDataError("Registry-only provider has no remote training jobs")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Training capability clock must return timezone-aware values")
        return value


class RegistryOnlyDeploymentController:
    """Prevent model traffic changes until an explicit control-plane Adapter exists."""

    async def deploy(
        self,
        artifact: ModelArtifact,
        *,
        stage: ModelLifecycleStage,
        traffic_percentage: float,
        operation_id: str,
        requested_by: str,
        previous_deployment: ModelDeployment | None,
    ) -> ModelDeployment:
        del (
            artifact,
            stage,
            traffic_percentage,
            operation_id,
            requested_by,
            previous_deployment,
        )
        raise TrainingDataError("A model deployment control plane is not configured")

    async def rollback(
        self,
        current: ModelDeployment,
        target: ModelDeployment,
        *,
        operation_id: str,
        requested_by: str,
    ) -> ModelDeployment:
        del current, target, operation_id, requested_by
        raise TrainingDataError("A model deployment control plane is not configured")
