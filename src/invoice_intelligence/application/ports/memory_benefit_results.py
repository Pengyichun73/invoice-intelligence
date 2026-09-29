"""Atomic PostgreSQL persistence of completed paired benefit evidence."""

from collections.abc import Sequence
from typing import Any, Protocol

from invoice_intelligence.application.ports.memory_effectiveness import PairedBenefitRun


class MemoryBenefitResultRepository(Protocol):
    async def get_run(self, tenant_id: str, run_id: str) -> PairedBenefitRun | None: ...

    async def save_completed(
        self, run: PairedBenefitRun, judgments: Sequence[dict[str, Any]]
    ) -> None: ...
