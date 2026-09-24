"""可信评估、模型产物和版本事实的只读边界。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    candidate_id: str
    tenant_id: str
    artifact_id: str
    model_evaluation_id: str
    evaluation_run_id: str
    dataset_version: str
    model_version: str
    prompt_version: str
    schema_version: str
    index_version: str
    threshold_version: str
    metric_values: dict[str, float]
    hard_failure_code: str | None
    compatibility_errors: tuple[str, ...]


class PromotionEvidenceRepository(Protocol):
    async def load(
        self,
        *,
        tenant_id: str,
        candidate_id: str,
        evaluation_run_id: str,
        artifact_id: str,
    ) -> PromotionEvidence: ...
