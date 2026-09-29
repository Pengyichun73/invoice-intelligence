"""Framework-independent governance and retrieval-observability contracts."""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from invoice_intelligence.domain.examples import (
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)


class MemoryPermission(StrEnum):
    """Permissions asserted by a trusted upstream identity boundary."""

    READ = "memory:read"
    GOVERN = "memory:govern"
    REBUILD_INDEX = "memory:rebuild_index"
    SUBMIT_FEEDBACK = "memory:submit_feedback"
    READ_EVALUATION = "memory:read_evaluation"
    SUBMIT_GOLD = "memory:submit_gold"
    ADJUDICATE_GOLD = "memory:adjudicate_gold"
    GOVERN_FIELD_ALIAS = "memory:govern_field_alias"
    GOVERN_GLOBAL_FIELD_ALIAS = "memory:govern_global_field_alias"


@dataclass(frozen=True, slots=True)
class TrustedTenantContext:
    """Tenant and actor identity established outside request-controlled payloads."""

    tenant_id: str
    actor_id: str
    permissions: frozenset[MemoryPermission]
    trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_normalized("tenant_id", self.tenant_id)
        _require_normalized("actor_id", self.actor_id)
        if self.trace_id is not None:
            _require_safe_trace_id(self.trace_id)

    def permits(self, permission: MemoryPermission) -> bool:
        return permission in self.permissions


class GovernanceAction(StrEnum):
    """Audited mutations exposed by the memory governance API."""

    APPROVE_ADMISSION = "approve_admission"
    REJECT_ADMISSION = "reject_admission"
    QUARANTINE_ADMISSION = "quarantine_admission"
    REQUEUE_ADMISSION = "requeue_admission"
    APPROVE_FIELD_ALIAS = "approve_field_alias"
    DISABLE_FIELD_ALIAS = "disable_field_alias"
    RESOLVE_CONFLICT = "resolve_conflict"
    DISMISS_CONFLICT = "dismiss_conflict"
    DISABLE_EXAMPLE = "disable_example"
    INVALIDATE_SCHEMA = "invalidate_schema"
    REBUILD_INDEX = "rebuild_index"
    SUBMIT_FEEDBACK = "submit_feedback"


class RetrievalFeedbackLabel(StrEnum):
    """Human relevance judgment; it does not relabel invoice field facts."""

    HELPFUL = "helpful"
    NOT_RELEVANT = "not_relevant"
    MISLEADING = "misleading"


@dataclass(frozen=True, slots=True)
class GovernanceAuditEvent:
    """Sensitive-value-free record of one attributable governance mutation."""

    audit_id: str
    tenant_id: str
    action: GovernanceAction
    resource_type: str
    resource_id: str
    reviewer_id: str
    reason: str
    resource_version: str | None
    trace_id: str | None
    idempotency_key_hash: str
    created_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("audit_id", self.audit_id),
            ("tenant_id", self.tenant_id),
            ("resource_type", self.resource_type),
            ("resource_id", self.resource_id),
            ("reviewer_id", self.reviewer_id),
            ("reason", self.reason),
            ("idempotency_key_hash", self.idempotency_key_hash),
        ):
            _require_normalized(name, value)
        if self.resource_version is not None:
            _require_normalized("resource_version", self.resource_version)
            if len(self.resource_version) > 256:
                raise ValueError("resource_version must not exceed 256 characters")
        if self.trace_id is not None:
            _require_safe_trace_id(self.trace_id)
        _require_aware("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class IndexProjectionCounts:
    """PostgreSQL projection-state counters for one rebuildable index version."""

    pending: int = 0
    processing: int = 0
    indexed: int = 0
    failed: int = 0
    invalidated: int = 0

    def __post_init__(self) -> None:
        if any(value < 0 for value in self.as_dict().values()):
            raise ValueError("Index projection counts must not be negative")

    @property
    def total(self) -> int:
        return sum(self.as_dict().values())

    def as_dict(self) -> dict[IndexProjectionStatus, int]:
        return {
            IndexProjectionStatus.PENDING: self.pending,
            IndexProjectionStatus.PROCESSING: self.processing,
            IndexProjectionStatus.INDEXED: self.indexed,
            IndexProjectionStatus.FAILED: self.failed,
            IndexProjectionStatus.INVALIDATED: self.invalidated,
        }


@dataclass(frozen=True, slots=True)
class IndexGovernanceRecord:
    """Version definition and PostgreSQL-owned projection lifecycle."""

    tenant_id: str
    index_version: IndexVersion
    schema_version: str
    dense_model_version: ModelVersion
    sparse_model_version: ModelVersion | None
    rerank_model_version: ModelVersion | None
    prompt_version: PromptVersion
    is_active: bool
    is_valid: bool
    invalidated_reason: str | None
    projection_counts: IndexProjectionCounts
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None
    invalidated_at: datetime | None

    def __post_init__(self) -> None:
        _require_normalized("tenant_id", self.tenant_id)
        _require_normalized("schema_version", self.schema_version)
        if self.invalidated_reason is not None:
            _require_normalized("invalidated_reason", self.invalidated_reason)
        for name, value in (
            ("created_at", self.created_at),
            ("activated_at", self.activated_at),
            ("retired_at", self.retired_at),
            ("invalidated_at", self.invalidated_at),
        ):
            if value is not None:
                _require_aware(name, value)


@dataclass(frozen=True, slots=True)
class RetrievalStageMetrics:
    """Non-sensitive wall-clock durations for each retrieval stage."""

    redaction_ms: float
    index_lookup_ms: float
    rewrite_ms: float
    dense_embedding_ms: float
    sparse_embedding_ms: float
    hybrid_search_ms: float
    rerank_ms: float
    total_ms: float

    def __post_init__(self) -> None:
        values = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("Retrieval stage durations must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """One scoped retrieval trace without invoice values or Prompt content."""

    trace_id: str
    tenant_id: str
    document_type: str
    field_path: str
    schema_version: str
    index_version: IndexVersion | None
    dense_model_version: ModelVersion
    sparse_model_version: ModelVersion | None
    rerank_model_version: ModelVersion | None
    prompt_version: PromptVersion
    retrieval_policy_version: RetrievalPolicyVersion
    threshold_version: str
    stage_metrics: RetrievalStageMetrics
    dense_candidate_count: int
    sparse_candidate_count: int
    rerank_candidate_count: int
    positive_result_count: int
    negative_result_count: int
    positive_example_ids: tuple[str, ...]
    negative_example_ids: tuple[str, ...]
    empty_retrieval: bool
    positive_hit_rate: float
    negative_hit_rate: float
    review_required: bool | None
    remote_model_error_count: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost: float | None
    succeeded: bool
    error_code: str | None
    created_at: datetime
    completed_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("trace_id", self.trace_id),
            ("tenant_id", self.tenant_id),
            ("document_type", self.document_type),
            ("field_path", self.field_path),
            ("schema_version", self.schema_version),
            ("threshold_version", self.threshold_version),
        ):
            _require_normalized(name, value)
        counts = (
            self.dense_candidate_count,
            self.sparse_candidate_count,
            self.rerank_candidate_count,
            self.positive_result_count,
            self.negative_result_count,
            self.remote_model_error_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Retrieval counters must not be negative")
        for rate_name, rate_value in (
            ("positive_hit_rate", self.positive_hit_rate),
            ("negative_hit_rate", self.negative_hit_rate),
        ):
            if not math.isfinite(rate_value) or not 0 <= rate_value <= 1:
                raise ValueError(f"{rate_name} must be between zero and one")
        for token_name, token_value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if token_value is not None and token_value < 0:
                raise ValueError(f"{token_name} must not be negative")
        if self.estimated_cost is not None and (
            not math.isfinite(self.estimated_cost) or self.estimated_cost < 0
        ):
            raise ValueError("estimated_cost must be finite and non-negative")
        if len(self.positive_example_ids) != len(set(self.positive_example_ids)):
            raise ValueError("Positive trace example identifiers must be unique")
        if len(self.negative_example_ids) != len(set(self.negative_example_ids)):
            raise ValueError("Negative trace example identifiers must be unique")
        if len(self.positive_example_ids) != self.positive_result_count:
            raise ValueError("Positive trace identifiers must match the result count")
        if len(self.negative_example_ids) != self.negative_result_count:
            raise ValueError("Negative trace identifiers must match the result count")
        for example_id in self.positive_example_ids + self.negative_example_ids:
            _require_normalized("trace example identifier", example_id)
        if set(self.positive_example_ids) & set(self.negative_example_ids):
            raise ValueError("Positive and negative trace examples must be disjoint")
        if self.empty_retrieval != (not (
            self.positive_result_count or self.negative_result_count
        )):
            raise ValueError("empty_retrieval does not match result counters")
        if self.succeeded and self.error_code is not None:
            raise ValueError("Successful retrieval trace cannot contain an error code")
        if not self.succeeded and self.error_code is None:
            raise ValueError("Failed retrieval trace requires a safe error code")
        _require_aware("created_at", self.created_at)
        _require_aware("completed_at", self.completed_at)
        if self.completed_at < self.created_at:
            raise ValueError("Retrieval trace completion cannot precede creation")


@dataclass(frozen=True, slots=True)
class RetrievalFeedback:
    """Attributable human judgment about one retrieved example."""

    feedback_id: str
    tenant_id: str
    trace_id: str
    example_id: str
    label: RetrievalFeedbackLabel
    reviewer_id: str
    reason: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("feedback_id", self.feedback_id),
            ("tenant_id", self.tenant_id),
            ("trace_id", self.trace_id),
            ("example_id", self.example_id),
            ("reviewer_id", self.reviewer_id),
        ):
            _require_normalized(name, value)
        if self.reason is not None:
            _require_normalized("reason", self.reason)
        if self.label is not RetrievalFeedbackLabel.HELPFUL and self.reason is None:
            raise ValueError("Negative retrieval feedback requires a reason")
        _require_aware("created_at", self.created_at)


@dataclass(frozen=True, slots=True)
class RetrievalMetricSummary:
    """Tenant/index aggregate computed from immutable retrieval traces."""

    trace_count: int
    empty_retrieval_rate: float
    positive_hit_rate: float
    negative_hit_rate: float
    review_required_rate: float | None
    remote_model_error_rate: float
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost: float | None

    def __post_init__(self) -> None:
        if self.trace_count < 0 or self.total_input_tokens < 0 or self.total_output_tokens < 0:
            raise ValueError("Retrieval summary counters must not be negative")
        for name, value in (
            ("empty_retrieval_rate", self.empty_retrieval_rate),
            ("positive_hit_rate", self.positive_hit_rate),
            ("negative_hit_rate", self.negative_hit_rate),
            ("review_required_rate", self.review_required_rate),
            ("remote_model_error_rate", self.remote_model_error_rate),
        ):
            if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
                raise ValueError(f"{name} must be between zero and one")
        if self.total_estimated_cost is not None and (
            not math.isfinite(self.total_estimated_cost)
            or self.total_estimated_cost < 0
        ):
            raise ValueError("Total estimated cost must be finite and non-negative")


def _require_normalized(name: str, value: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty and normalized")


def _require_safe_trace_id(value: str) -> None:
    if not 1 <= len(value) <= 64 or value != value.strip():
        raise ValueError("trace_id must be a normalized value of at most 64 characters")
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    )
    if any(character not in allowed for character in value):
        raise ValueError("trace_id contains unsupported characters")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
