"""Safe, aggregate-only contracts for isolated offline evaluation jobs."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from invoice_intelligence.domain.evaluation import EvaluationSuite


class EvaluationJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    QUARANTINED = "quarantined"


EVALUATION_VARIANTS = (
    "no_memory", "pgvector_legacy", "dense_only", "sparse_only",
    "hybrid", "hybrid_reranker",
)


@dataclass(frozen=True, slots=True)
class SnapshotCase:
    """Synthetic or manually de-identified judgments; never an invoice value."""

    case_id: str
    field_path: str
    expected_present: bool
    predicted_present: bool
    field_correct: bool
    evidence_covered: bool
    amount_absolute_error: float | None
    review_required: bool
    negative_false_recall: bool
    ocr_vision_consistent: bool

    def __post_init__(self) -> None:
        if not self.case_id.strip() or not self.field_path.strip():
            raise ValueError("Snapshot case identity is required")
        if self.field_correct and not (self.expected_present and self.predicted_present):
            raise ValueError("Correct fields must be present on both sides")
        if self.amount_absolute_error is not None and (
            self.amount_absolute_error < 0 or self.amount_absolute_error > 1_000_000
        ):
            raise ValueError("Amount error must be bounded and non-negative")


@dataclass(frozen=True, slots=True)
class DatasetSnapshot:
    snapshot_id: str
    tenant_id: str
    dataset_version: str
    schema_version: str
    cases: tuple[SnapshotCase, ...]
    content_sha256: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.cases or len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Snapshot requires unique cases")
        if self.created_at.tzinfo is None:
            raise ValueError("Snapshot timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class EvaluationJob:
    job_id: str
    tenant_id: str
    snapshot_id: str | None
    dataset_version: str
    schema_version: str
    index_version: str
    model_version: str
    prompt_version: str
    threshold_version: str
    status: EvaluationJobStatus
    attempt_count: int
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    worker_id: str | None
    lease_token: str | None
    failure_code: str | None
    report: dict[str, object] | None
    created_at: datetime
    updated_at: datetime
    evidence_class: str = "diagnostic_only"
    dataset_id: str | None = None
    suite: EvaluationSuite | None = None
    retrieval_policy_version: str | None = None
    catalog_version: str | None = None
    admission_policy_version: str | None = None
    field_binding_policy_version: str | None = None
    evaluation_run_id: str | None = None

    def __post_init__(self) -> None:
        if self.evidence_class == "diagnostic_only":
            if (
                self.snapshot_id is None or self.dataset_id is not None
                or self.suite is not None or self.evaluation_run_id is not None
            ):
                raise ValueError("Diagnostic evaluation job requires only a Snapshot")
        elif self.evidence_class == "suite_run":
            if (
                self.snapshot_id is not None
                or not self.dataset_id
                or self.suite is None
                or not self.retrieval_policy_version
            ):
                raise ValueError("Suite evaluation job requires immutable dataset bindings")
            if self.suite is EvaluationSuite.TRUSTED_MEMORY_FIELD_BINDING and any(
                not value for value in (
                    self.catalog_version,
                    self.admission_policy_version,
                    self.field_binding_policy_version,
                )
            ):
                raise ValueError("Trusted-memory Suite requires catalog and policy versions")
            if (self.status is EvaluationJobStatus.COMPLETED) != (
                self.evaluation_run_id is not None
            ):
                raise ValueError("Completed Suite Job requires an evaluation Run binding")
        elif self.evidence_class == "memory_benefit":
            if (
                self.snapshot_id is not None or not self.dataset_id
                or self.suite is not None or not self.catalog_version
                or self.retrieval_policy_version is not None
                or (self.status is EvaluationJobStatus.COMPLETED)
                != (self.evaluation_run_id is not None)
            ):
                raise ValueError("Memory benefit Job requires immutable gold bindings")
        else:
            raise ValueError("Evaluation evidence class is invalid")


@dataclass(frozen=True, slots=True)
class EvaluationSchedule:
    schedule_id: str
    tenant_id: str
    snapshot_id: str
    index_version: str
    model_version: str
    prompt_version: str
    threshold_version: str
    interval_seconds: int
    next_run_at: datetime
    enabled: bool


def calculate_stub_metrics(cases: tuple[SnapshotCase, ...]) -> dict[str, float | int | None]:
    """Calculate transparent metrics from de-identified judgments, not model output."""

    total = len(cases)
    positives = sum(case.expected_present for case in cases)
    true_positive = sum(case.expected_present and case.field_correct for case in cases)
    amount_errors = [
        case.amount_absolute_error for case in cases
        if case.amount_absolute_error is not None
    ]
    return {
        "case_count": total,
        "field_accuracy": sum(case.field_correct for case in cases) / total,
        "field_recall": true_positive / positives if positives else None,
        "missing_rate": sum(not case.predicted_present for case in cases) / total,
        "evidence_coverage": sum(case.evidence_covered for case in cases) / total,
        "amount_mean_absolute_error": (
            sum(amount_errors) / len(amount_errors) if amount_errors else None
        ),
        "human_review_rate": sum(case.review_required for case in cases) / total,
        "negative_false_recall_rate": sum(case.negative_false_recall for case in cases) / total,
        "ocr_vision_consistency_rate": sum(
            case.ocr_vision_consistent for case in cases
        ) / total,
    }
