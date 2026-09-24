"""Framework-independent contracts for trusted memory admission."""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from invoice_intelligence.domain.examples import ModelVersion, PromptVersion
from invoice_intelligence.domain.workflow import SignalVerdict


class MemoryAdmissionStatus(StrEnum):
    """Lifecycle independent from source review and record validity."""

    PENDING = "pending"
    APPROVED = "approved"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    INVALIDATED = "invalidated"


class MemoryAssessmentSource(StrEnum):
    """Origin of an assessment; neither source can persist a final decision."""

    DETERMINISTIC = "deterministic"
    MODEL_ADVISORY = "model_advisory"


class MemoryAdmissionRecommendation(StrEnum):
    """Non-authoritative recommendation produced by an assessment."""

    RECOMMEND_APPROVAL = "recommend_approval"
    RECOMMEND_QUARANTINE = "recommend_quarantine"
    RECOMMEND_REJECTION = "recommend_rejection"


class MemoryAdmissionDecisionAuthority(StrEnum):
    """Authorities allowed to make a final decision; model is deliberately absent."""

    DETERMINISTIC_POLICY = "deterministic_policy"
    HUMAN_GOVERNOR = "human_governor"


class MemoryConflictStatus(StrEnum):
    """Lifecycle of a conflict without mutating its source review facts."""

    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class MemoryConflictReevaluationTargetType(StrEnum):
    """Governance target that must be reconsidered after a conflict is closed."""

    MEMORY_ADMISSION = "memory_admission"
    FIELD_ALIAS = "field_alias"


@dataclass(frozen=True, slots=True)
class MemoryQualitySignal:
    """One auditable quality signal; score is normalized, not a probability."""

    code: str
    source: MemoryAssessmentSource
    verdict: SignalVerdict
    score: float | None
    message: str
    field_path: str | None
    evidence_references: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_normalized("signal code", self.code)
        _require_normalized("signal message", self.message)
        if self.field_path is not None:
            _require_normalized("signal field_path", self.field_path)
        _validate_optional_score("signal score", self.score)
        _require_unique_text("signal evidence reference", self.evidence_references)


@dataclass(frozen=True, slots=True)
class MemoryQualityAssessment:
    """Versioned assessment kept separate from the final admission decision."""

    assessment_id: str
    tenant_id: str
    example_id: str
    source: MemoryAssessmentSource
    signals: tuple[MemoryQualitySignal, ...]
    quality_score: float
    recommendation: MemoryAdmissionRecommendation
    reason_codes: tuple[str, ...]
    policy_version: str
    input_fingerprint: str
    created_at: datetime
    model_version: ModelVersion | None
    prompt_version: PromptVersion | None
    advisory_only: Literal[True] = True

    def __post_init__(self) -> None:
        for name, value in (
            ("assessment_id", self.assessment_id),
            ("tenant_id", self.tenant_id),
            ("example_id", self.example_id),
            ("policy_version", self.policy_version),
            ("input_fingerprint", self.input_fingerprint),
        ):
            _require_normalized(name, value)
        if not self.signals:
            raise ValueError("Memory quality assessment requires at least one signal")
        if any(signal.source is not self.source for signal in self.signals):
            raise ValueError("Assessment signals must match the assessment source")
        _validate_score("quality_score", self.quality_score)
        if not self.reason_codes:
            raise ValueError("Memory quality assessment requires at least one reason code")
        _require_unique_text("assessment reason code", self.reason_codes)
        _require_aware("assessment created_at", self.created_at)
        versions = (self.model_version, self.prompt_version)
        if self.source is MemoryAssessmentSource.MODEL_ADVISORY and any(
            version is None for version in versions
        ):
            raise ValueError("Model assessments require model and Prompt versions")
        if self.source is MemoryAssessmentSource.DETERMINISTIC and any(
            version is not None for version in versions
        ):
            raise ValueError("Deterministic assessments cannot declare model versions")
        if self.advisory_only is not True:
            raise ValueError("Memory quality assessments are advisory only")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionDecision:
    """Final, attributable state transition; never produced by a model provider."""

    decision_id: str
    tenant_id: str
    example_id: str
    previous_status: MemoryAdmissionStatus | None
    status: MemoryAdmissionStatus
    authority: MemoryAdmissionDecisionAuthority
    decided_by: str
    reason: str
    reason_codes: tuple[str, ...]
    assessment_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    policy_version: str
    idempotency_key_hash: str
    revision: int
    decided_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("decision_id", self.decision_id),
            ("tenant_id", self.tenant_id),
            ("example_id", self.example_id),
            ("decided_by", self.decided_by),
            ("reason", self.reason),
            ("policy_version", self.policy_version),
            ("idempotency_key_hash", self.idempotency_key_hash),
        ):
            _require_normalized(name, value)
        _require_unique_text("decision reason code", self.reason_codes)
        _require_unique_text("decision assessment id", self.assessment_ids)
        _require_unique_text("decision conflict id", self.conflict_ids)
        if not self.reason_codes:
            raise ValueError("Admission decision requires at least one reason code")
        if self.revision <= 0:
            raise ValueError("Admission decision revision must be greater than zero")
        if self.previous_status is None and self.status is not MemoryAdmissionStatus.PENDING:
            raise ValueError("The first admission decision must create pending status")
        if self.previous_status is None and self.revision != 1:
            raise ValueError("The first admission decision must have revision one")
        if self.previous_status is not None and self.revision == 1:
            raise ValueError("A state transition must advance beyond revision one")
        if self.previous_status is self.status:
            raise ValueError("Admission decision must change status")
        _require_aware("decision decided_at", self.decided_at)


@dataclass(frozen=True, slots=True)
class MemoryAdmissionRecord:
    """Current admission state for one reviewed example in PostgreSQL."""

    tenant_id: str
    example_id: str
    status: MemoryAdmissionStatus
    current_decision_id: str
    policy_version: str
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("example_id", self.example_id),
            ("current_decision_id", self.current_decision_id),
            ("policy_version", self.policy_version),
        ):
            _require_normalized(name, value)
        if self.revision <= 0:
            raise ValueError("Admission revision must be greater than zero")
        _require_aware("admission created_at", self.created_at)
        _require_aware("admission updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("Admission updated_at cannot precede created_at")

    @property
    def eligible_for_long_term_retrieval(self) -> bool:
        """Only approved records may be projected or retrieved."""

        return self.status is MemoryAdmissionStatus.APPROVED


@dataclass(frozen=True, slots=True)
class MemoryConflictRecord:
    """Reference-only conflict evidence; source reviews and events remain immutable."""

    conflict_id: str
    tenant_id: str
    example_ids: tuple[str, ...]
    document_type: str
    field_path: str
    schema_version: str
    conflict_type: str
    fingerprint: str
    reason_codes: tuple[str, ...]
    evidence_references: tuple[str, ...]
    status: MemoryConflictStatus
    detected_at: datetime
    resolved_at: datetime | None
    resolution_decision_id: str | None
    field_alias_candidate_ids: tuple[str, ...] = ()
    candidate_field_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (
            ("conflict_id", self.conflict_id),
            ("tenant_id", self.tenant_id),
            ("document_type", self.document_type),
            ("field_path", self.field_path),
            ("schema_version", self.schema_version),
            ("conflict_type", self.conflict_type),
            ("fingerprint", self.fingerprint),
        ):
            _require_normalized(name, value)
        if bool(self.example_ids) == bool(self.field_alias_candidate_ids):
            raise ValueError(
                "Memory conflict requires reviewed examples or field alias candidates"
            )
        _require_unique_text("conflict example id", self.example_ids)
        _require_unique_text(
            "conflict field alias candidate id",
            self.field_alias_candidate_ids,
        )
        _require_unique_text("conflict candidate field path", self.candidate_field_paths)
        if self.field_alias_candidate_ids and not self.candidate_field_paths:
            raise ValueError("Field alias conflicts require candidate field paths")
        if self.field_alias_candidate_ids and len(self.candidate_field_paths) < 2:
            raise ValueError("Field alias conflicts require at least two candidate fields")
        if self.example_ids and self.candidate_field_paths:
            raise ValueError("Reviewed-example conflicts cannot carry alias field paths")
        _require_unique_text("conflict reason code", self.reason_codes)
        _require_unique_text("conflict evidence reference", self.evidence_references)
        if not self.reason_codes:
            raise ValueError("Memory conflict requires at least one reason code")
        _require_aware("conflict detected_at", self.detected_at)
        is_open = self.status is MemoryConflictStatus.OPEN
        if is_open and (
            self.resolved_at is not None or self.resolution_decision_id is not None
        ):
            raise ValueError("Open conflicts cannot contain resolution metadata")
        if not is_open and (
            self.resolved_at is None or self.resolution_decision_id is None
        ):
            raise ValueError("Closed conflicts require resolution metadata")
        if self.resolved_at is not None:
            _require_aware("conflict resolved_at", self.resolved_at)
            if self.resolved_at < self.detected_at:
                raise ValueError("Conflict resolved_at cannot precede detected_at")
        if self.resolution_decision_id is not None:
            _require_normalized(
                "conflict resolution_decision_id",
                self.resolution_decision_id,
            )


@dataclass(frozen=True, slots=True)
class MemoryConflictReevaluationTarget:
    """Durable pending target; registration never changes its governed state."""

    target_type: MemoryConflictReevaluationTargetType
    target_id: str

    def __post_init__(self) -> None:
        _require_normalized("conflict reevaluation target_id", self.target_id)


@dataclass(frozen=True, slots=True)
class MemoryConflictResolutionDecision:
    """Immutable human decision that closes a conflict without approving its sources."""

    resolution_decision_id: str
    tenant_id: str
    conflict_id: str
    previous_status: MemoryConflictStatus
    target_status: MemoryConflictStatus
    selected_canonical_field_path: str | None
    reviewer_id: str
    reason: str
    resolution_note: str | None
    idempotency_key_hash: str
    policy_version: str
    decided_at: datetime
    reevaluation_targets: tuple[MemoryConflictReevaluationTarget, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("resolution_decision_id", self.resolution_decision_id),
            ("tenant_id", self.tenant_id),
            ("conflict_id", self.conflict_id),
            ("reviewer_id", self.reviewer_id),
            ("reason", self.reason),
            ("idempotency_key_hash", self.idempotency_key_hash),
            ("policy_version", self.policy_version),
        ):
            _require_normalized(name, value)
        if self.previous_status is not MemoryConflictStatus.OPEN:
            raise ValueError("Conflict resolution previous status must be open")
        if self.target_status not in {
            MemoryConflictStatus.RESOLVED,
            MemoryConflictStatus.DISMISSED,
        }:
            raise ValueError("Conflict resolution target status must close the conflict")
        if self.selected_canonical_field_path is not None:
            _require_normalized(
                "selected_canonical_field_path",
                self.selected_canonical_field_path,
            )
        if (
            self.target_status is MemoryConflictStatus.DISMISSED
            and self.selected_canonical_field_path is not None
        ):
            raise ValueError("Dismissed conflicts cannot select a canonical field path")
        if self.resolution_note is not None:
            _require_normalized("resolution_note", self.resolution_note)
        if not self.reevaluation_targets:
            raise ValueError("Conflict resolution requires reevaluation targets")
        target_keys = tuple(
            (item.target_type, item.target_id) for item in self.reevaluation_targets
        )
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("Conflict reevaluation targets must be unique")
        _require_aware("conflict resolution decided_at", self.decided_at)


@dataclass(frozen=True, slots=True)
class ReviewerReliabilityProfile:
    """Versioned quality signal that cannot independently approve a memory."""

    tenant_id: str
    reviewer_id: str
    profile_version: str
    policy_version: str
    reviewed_fact_count: int
    approved_fact_count: int
    quarantined_fact_count: int
    rejected_fact_count: int
    conflict_count: int
    reliability_score: float
    minimum_sample_met: bool
    calculated_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("reviewer_id", self.reviewer_id),
            ("profile_version", self.profile_version),
            ("policy_version", self.policy_version),
        ):
            _require_normalized(name, value)
        counts = (
            self.reviewed_fact_count,
            self.approved_fact_count,
            self.quarantined_fact_count,
            self.rejected_fact_count,
            self.conflict_count,
        )
        if any(count < 0 for count in counts):
            raise ValueError("Reviewer reliability counts must not be negative")
        if (
            self.approved_fact_count
            + self.quarantined_fact_count
            + self.rejected_fact_count
            > self.reviewed_fact_count
        ):
            raise ValueError("Admission outcome counts cannot exceed reviewed facts")
        _validate_score("reliability_score", self.reliability_score)
        _require_aware("profile calculated_at", self.calculated_at)


def _require_normalized(name: str, value: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty and normalized")


def _require_unique_text(name: str, values: tuple[str, ...]) -> None:
    if any(not value.strip() or value != value.strip() for value in values):
        raise ValueError(f"{name} values must be non-empty and normalized")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} values must be unique")


def _validate_score(name: str, value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be finite and between zero and one")


def _validate_optional_score(name: str, value: float | None) -> None:
    if value is not None:
        _validate_score(name, value)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
