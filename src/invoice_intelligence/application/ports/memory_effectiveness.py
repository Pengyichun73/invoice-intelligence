"""Read-only facts used to judge memory operation and measured benefit."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class EffectivenessFacts:
    document_count: int
    extraction_run_count: int
    reviewed_example_count: int
    pending_admission_count: int
    approved_admission_count: int
    indexed_example_count: int
    retrieval_count: int
    nonempty_retrieval_count: int
    review_required_retrieval_count: int
    active_index_version: str | None
    latest_document_at: datetime | None
    latest_extraction_at: datetime | None
    latest_review_at: datetime | None
    latest_admission_at: datetime | None
    latest_projection_at: datetime | None
    latest_retrieval_at: datetime | None
    evaluation_job_count: int
    completed_evaluation_job_count: int
    latest_evaluation_job_at: datetime | None
    gold_case_count: int
    frozen_gold_case_count: int
    frozen_template_group_count: int
    latest_frozen_gold_at: datetime | None


@dataclass(frozen=True, slots=True)
class PairedBenefitRun:
    run_id: str
    tenant_id: str
    status: str
    dataset_digest: str
    schema_version: str
    catalog_version: str
    index_version: str
    model_version: str
    prompt_version: str
    case_count: int
    template_group_count: int
    metrics: dict[str, object]
    scenarios: tuple[dict[str, object], ...]
    blocker_codes: tuple[str, ...]
    created_at: datetime
    completed_at: datetime | None


class MemoryEffectivenessRepository(Protocol):
    async def facts(
        self, tenant_id: str, since: datetime, until: datetime
    ) -> EffectivenessFacts: ...

    async def latest_run(self, tenant_id: str) -> PairedBenefitRun | None: ...

    async def get_run(self, tenant_id: str, run_id: str) -> PairedBenefitRun | None: ...
