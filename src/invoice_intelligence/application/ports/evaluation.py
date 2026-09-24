"""Application boundaries for tenant-scoped offline evaluation."""

from collections.abc import Sequence
from typing import Protocol

from invoice_intelligence.domain.evaluation import (
    EvaluationBindings,
    EvaluationCase,
    EvaluationCaseObservation,
    EvaluationDataset,
    EvaluationRun,
    EvaluationVariant,
)


class EvaluationDatasetRepository(Protocol):
    """Persist immutable evaluation datasets in the PostgreSQL fact source."""

    async def add(self, dataset: EvaluationDataset) -> None:
        """Idempotently persist one frozen dataset version."""

        ...

    async def get_dataset(
        self,
        tenant_id: str,
        dataset_id: str,
        dataset_version: str,
    ) -> EvaluationDataset | None:
        """Read one exact tenant dataset version without cross-tenant fallback."""

        ...


class EvaluationRunRepository(Protocol):
    """Persist version bindings, metrics, and promotion proposals in PostgreSQL."""

    async def save(self, run: EvaluationRun) -> None:
        """Create or advance one run while preserving immutable bindings."""

        ...

    async def get_run(
        self,
        tenant_id: str,
        evaluation_run_id: str,
    ) -> EvaluationRun | None:
        """Read one tenant-owned evaluation run."""

        ...


class EvaluationVariantRunner(Protocol):
    """One explicitly configured Suite variant with no production mutations."""

    async def evaluate_case(
        self,
        case: EvaluationCase,
        dataset: EvaluationDataset,
        bindings: EvaluationBindings,
    ) -> EvaluationCaseObservation:
        """Return retrieval, extraction, admission, and/or binding observations."""

        ...


class EvaluationVariantExecutor(Protocol):
    """Execute one configured variant against a frozen held-out dataset."""

    async def evaluate(
        self,
        dataset: EvaluationDataset,
        variant: EvaluationVariant,
        bindings: EvaluationBindings,
    ) -> Sequence[EvaluationCaseObservation]:
        """Return exactly one observation per case without changing production state."""

        ...


class EvaluationArtifactPublisher(Protocol):
    """Publish derived machine and human reports from a completed PostgreSQL run."""

    async def publish(self, run: EvaluationRun) -> tuple[str, ...]:
        """Return stable references for JSON and human-review report artifacts."""

        ...
