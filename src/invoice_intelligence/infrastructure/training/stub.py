"""Deterministic fail-closed training provider used by default."""

from invoice_intelligence.application.errors import TrainingProviderPermanentError
from invoice_intelligence.domain.training_registry import (
    TrainingProviderRequest,
    TrainingProviderSnapshot,
)


class DeterministicStubTrainingProvider:
    """Never pretends that remote training ran or succeeded."""

    async def submit(
        self, request: TrainingProviderRequest, *, operation_id: str
    ) -> TrainingProviderSnapshot:
        del request, operation_id
        raise TrainingProviderPermanentError("training_provider_not_configured")

    async def get_job(self, provider_job_id: str) -> TrainingProviderSnapshot:
        del provider_job_id
        raise TrainingProviderPermanentError("training_provider_not_configured")

    async def cancel(
        self, provider_job_id: str, *, operation_id: str
    ) -> TrainingProviderSnapshot:
        del provider_job_id, operation_id
        raise TrainingProviderPermanentError("training_provider_not_configured")
