"""Application boundaries for trusted reviewed-example admission."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecision,
    MemoryAdmissionRecord,
    MemoryAdmissionStatus,
    MemoryAssessmentSource,
    MemoryConflictRecord,
    MemoryConflictResolutionDecision,
    MemoryConflictStatus,
    MemoryQualityAssessment,
    MemoryQualitySignal,
    ReviewerReliabilityProfile,
)
from invoice_intelligence.domain.document import VisionImage
from invoice_intelligence.domain.examples import ModelVersion, PromptVersion, ReviewedExample
from invoice_intelligence.domain.extraction import EvidenceSource, FieldEvidence
from invoice_intelligence.domain.governance import GovernanceAuditEvent
from invoice_intelligence.domain.workflow import JsonValue


@dataclass(frozen=True, slots=True)
class MemoryFieldSchemaInspection:
    """Framework-neutral result of validating one reviewed field value."""

    document_type_exists: bool
    field_path_exists: bool
    value_type_valid: bool
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.error_code is not None and (
            not self.error_code.strip() or self.error_code != self.error_code.strip()
        ):
            raise ValueError("Schema inspection error_code must be normalized")
        if not self.document_type_exists and self.field_path_exists:
            raise ValueError("A field cannot exist when its document type is unknown")
        if not self.field_path_exists and self.value_type_valid:
            raise ValueError("A value cannot be valid when its field path is unknown")


class MemoryFieldSchemaInspector(Protocol):
    """Inspect a field against the active Entity Schema without exposing Pydantic."""

    def inspect_field(
        self,
        output_schema: type[object],
        document_type: str,
        field_path: str,
        value: JsonValue,
    ) -> MemoryFieldSchemaInspection:
        """Return path and value-type validity for one concrete Schema variant."""

        ...

    def describe_field(
        self,
        output_schema: type[object],
        document_type: str,
        field_path: str,
    ) -> str:
        """Return the current Entity field semantics without exposing Pydantic."""

        ...


@dataclass(frozen=True, slots=True)
class MemorySupportDiversity:
    """Tenant-scoped support counts; none is a correctness probability."""

    distinct_documents: int
    distinct_templates: int
    distinct_reviewers: int

    def __post_init__(self) -> None:
        if min(
            self.distinct_documents,
            self.distinct_templates,
            self.distinct_reviewers,
        ) < 0:
            raise ValueError("Memory support diversity counts must not be negative")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionWorkLease:
    """Opaque PostgreSQL-backed lease for one admission processing attempt."""

    tenant_id: str
    example_id: str
    worker_id: str
    lease_token: str
    attempt_count: int
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_id", self.tenant_id),
            ("example_id", self.example_id),
            ("worker_id", self.worker_id),
            ("lease_token", self.lease_token),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if self.attempt_count <= 0:
            raise ValueError("attempt_count must be greater than zero")
        if (
            self.lease_expires_at.tzinfo is None
            or self.lease_expires_at.utcoffset() is None
        ):
            raise ValueError("lease_expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MemoryQualityAssessmentImage:
    """Transient pixels paired with a durable evidence reference; never persist bytes."""

    evidence_reference: str
    image: VisionImage

    def __post_init__(self) -> None:
        if (
            not self.evidence_reference.strip()
            or self.evidence_reference != self.evidence_reference.strip()
        ):
            raise ValueError("Assessment image evidence_reference must be normalized")
        if self.image.page_number <= 0:
            raise ValueError("Assessment image page_number must be greater than zero")
        if self.image.width <= 0 or self.image.height <= 0:
            raise ValueError("Assessment image dimensions must be greater than zero")


@dataclass(frozen=True, slots=True)
class MemoryQualityAssessmentRequest:
    """Strict input envelope for a non-authoritative quality assessment."""

    request_id: str
    tenant_id: str
    example_id: str
    document_type: str
    field_path: str
    schema_version: str
    current_field_evidence: FieldEvidence
    temporary_images: tuple[MemoryQualityAssessmentImage, ...]
    model_value: JsonValue
    reviewed_value: JsonValue
    field_semantic_definition: str
    deterministic_signals: tuple[MemoryQualitySignal, ...]
    mandatory_business_rules: tuple[str, ...]
    policy_version: str
    requested_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("request_id", self.request_id),
            ("tenant_id", self.tenant_id),
            ("example_id", self.example_id),
            ("document_type", self.document_type),
            ("field_path", self.field_path),
            ("schema_version", self.schema_version),
            ("policy_version", self.policy_version),
            ("field_semantic_definition", self.field_semantic_definition),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if self.current_field_evidence.field_path != self.field_path:
            raise ValueError("Current field evidence must match the assessment field_path")
        if self.current_field_evidence.source is not EvidenceSource.VISUAL:
            raise ValueError("Memory assessment accepts only current visual field evidence")
        if not self.deterministic_signals:
            raise ValueError("Assessment request requires deterministic quality signals")
        if any(
            signal.source is not MemoryAssessmentSource.DETERMINISTIC
            for signal in self.deterministic_signals
        ):
            raise ValueError("Assessment request accepts only deterministic input signals")
        if not self.mandatory_business_rules:
            raise ValueError("Assessment request requires mandatory business rules")
        if any(
            not rule.strip() or rule != rule.strip()
            for rule in self.mandatory_business_rules
        ):
            raise ValueError("Mandatory business rules must be non-empty and normalized")
        if len(self.mandatory_business_rules) != len(set(self.mandatory_business_rules)):
            raise ValueError("Mandatory business rules must be unique")
        references = tuple(item.evidence_reference for item in self.temporary_images)
        if len(references) != len(set(references)):
            raise ValueError("Assessment image evidence references must be unique")
        evidence_page = self.current_field_evidence.page_number
        if self.temporary_images and evidence_page is not None and not any(
            item.image.page_number == evidence_page for item in self.temporary_images
        ):
            raise ValueError("Assessment images must include the current evidence page")
        if self.requested_at.tzinfo is None or self.requested_at.utcoffset() is None:
            raise ValueError("Assessment requested_at must be timezone-aware")


class MemoryQualityAssessmentProvider(Protocol):
    """Return a structured advisory assessment without admission authority.

    Remote implementations own timeout, retry, rate-limit, circuit-breaker,
    redaction verification and sensitive-value-free audit behavior.
    """

    @property
    def model_version(self) -> ModelVersion:
        """Return the immutable assessment model version used for replay detection."""

        ...

    @property
    def prompt_version(self) -> PromptVersion:
        """Return the immutable assessment Prompt version used for replay detection."""

        ...

    async def assess(
        self,
        request: MemoryQualityAssessmentRequest,
    ) -> MemoryQualityAssessment:
        """Assess redacted review facts; never persist an admission decision."""

        ...


class MemoryAdmissionRepository(Protocol):
    """Persist admission state and audit history in PostgreSQL."""

    async def create_pending(
        self,
        initial_decision: MemoryAdmissionDecision,
    ) -> MemoryAdmissionRecord:
        """Atomically create pending state and its immutable initial decision."""

        ...

    async def get(
        self,
        tenant_id: str,
        example_id: str,
    ) -> MemoryAdmissionRecord | None:
        """Read one tenant-owned admission record."""

        ...

    async def save_assessment(
        self,
        assessment: MemoryQualityAssessment,
    ) -> MemoryQualityAssessment:
        """Idempotently append an immutable quality assessment."""

        ...

    async def list_assessments(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryQualityAssessment, ...]:
        """Read assessments in stable creation order."""

        ...

    async def list_decisions(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryAdmissionDecision, ...]:
        """Read immutable decisions in revision order for audit."""

        ...

    async def append_decision(
        self,
        decision: MemoryAdmissionDecision,
        *,
        expected_revision: int,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> MemoryAdmissionRecord:
        """Atomically append a decision and update current state with optimistic locking."""

        ...

    async def claim_due(
        self,
        worker_id: str,
        *,
        now: datetime,
        lease_expires_at: datetime,
        limit: int,
    ) -> tuple[MemoryAdmissionWorkLease, ...]:
        """Atomically claim due work with PostgreSQL row locks and skip locked."""

        ...

    async def complete_claim(
        self,
        lease: MemoryAdmissionWorkLease,
        *,
        error_code: str | None = None,
        error_at: datetime | None = None,
    ) -> bool:
        """Finish matching work and optionally preserve a sanitized terminal error."""

        ...

    async def retry_claim(
        self,
        lease: MemoryAdmissionWorkLease,
        *,
        next_attempt_at: datetime,
        error_code: str,
        error_at: datetime,
    ) -> bool:
        """Release a matching lease and durably schedule its next attempt."""

        ...

    async def filter_approved_example_ids(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
    ) -> tuple[str, ...]:
        """Return only approved IDs, preserving caller order and tenant isolation."""

        ...

    async def list_approved_example_ids(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[str, ...]:
        """Page the only examples eligible for projection, retrieval or datasets."""

        ...

    async def list_by_status(
        self,
        tenant_id: str,
        statuses: Sequence[MemoryAdmissionStatus],
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        """Page tenant-owned records for governance without changing eligibility."""

        ...

    async def list_for_schema(
        self,
        tenant_id: str,
        schema_version: str,
        *,
        limit: int,
        after_example_id: str | None = None,
    ) -> tuple[MemoryAdmissionRecord, ...]:
        """Page admission records for explicit Schema invalidation decisions."""

        ...

    async def delete_tenant(self, tenant_id: str) -> int:
        """Delete tenant admission state without deleting original review facts."""

        ...

    async def purge_terminal_before(
        self,
        tenant_id: str,
        older_than: datetime,
    ) -> int:
        """Purge expired rejected/invalidated admission audits under retention policy."""

        ...


@dataclass(frozen=True, slots=True)
class MemoryAdmissionPolicyContext:
    """Local policy input; the raw review fact never crosses into a model Provider."""

    admission: MemoryAdmissionRecord
    reviewed_example: ReviewedExample
    assessments: tuple[MemoryQualityAssessment, ...]
    conflicts: tuple[MemoryConflictRecord, ...]
    reviewer_profile: ReviewerReliabilityProfile | None

    def __post_init__(self) -> None:
        if (
            self.admission.tenant_id != self.reviewed_example.tenant_id
            or self.admission.example_id != self.reviewed_example.example_id
        ):
            raise ValueError("Admission and reviewed example must have the same scope")
        if any(
            assessment.tenant_id != self.admission.tenant_id
            or assessment.example_id != self.admission.example_id
            for assessment in self.assessments
        ):
            raise ValueError("Assessments must belong to the admission scope")
        if any(
            conflict.tenant_id != self.admission.tenant_id
            or self.admission.example_id not in conflict.example_ids
            for conflict in self.conflicts
        ):
            raise ValueError("Conflicts must belong to the admission scope")
        if self.reviewer_profile is not None and (
            self.reviewer_profile.tenant_id != self.admission.tenant_id
            or self.reviewer_profile.reviewer_id != self.reviewed_example.reviewer_id
        ):
            raise ValueError("Reviewer profile must match the reviewed example")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionPolicyOutcome:
    """Policy proposal used to construct, but distinct from, a final decision."""

    target_status: MemoryAdmissionStatus
    reason_codes: tuple[str, ...]
    supporting_assessment_ids: tuple[str, ...]
    supporting_conflict_ids: tuple[str, ...]
    requires_human_decision: bool

    def __post_init__(self) -> None:
        for name, values in (
            ("reason code", self.reason_codes),
            ("assessment id", self.supporting_assessment_ids),
            ("conflict id", self.supporting_conflict_ids),
        ):
            if any(not value.strip() or value != value.strip() for value in values):
                raise ValueError(f"Admission policy {name}s must be non-empty and normalized")
            if len(values) != len(set(values)):
                raise ValueError(f"Admission policy {name}s must be unique")
        if not self.reason_codes:
            raise ValueError("Admission policy outcome requires at least one reason code")


class MemoryAdmissionPolicy(Protocol):
    """Apply deterministic gates around advisory assessments."""

    def evaluate(
        self,
        context: MemoryAdmissionPolicyContext,
    ) -> MemoryAdmissionPolicyOutcome:
        """Return a proposal; callers create and persist the attributable decision."""

        ...


class MemoryConflictRepository(Protocol):
    """Persist conflicts separately from immutable review facts and correction events."""

    async def save(self, conflict: MemoryConflictRecord) -> MemoryConflictRecord:
        """Idempotently persist one conflict fingerprint."""

        ...

    async def get(
        self,
        tenant_id: str,
        conflict_id: str,
    ) -> MemoryConflictRecord | None:
        """Read one tenant-owned conflict."""

        ...

    async def list_open_for_example(
        self,
        tenant_id: str,
        example_id: str,
    ) -> tuple[MemoryConflictRecord, ...]:
        """Return unresolved conflicts that must block automatic admission."""

        ...

    async def list_open_for_alias_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> tuple[MemoryConflictRecord, ...]:
        """Return unresolved conflicts blocking field alias promotion."""

        ...

    async def list_for_governance(
        self,
        tenant_id: str,
        statuses: Sequence[MemoryConflictStatus],
        *,
        alias_conflicts_only: bool,
        limit: int,
        after_conflict_id: str | None = None,
    ) -> tuple[MemoryConflictRecord, ...]:
        """Page tenant conflicts without exposing another tenant's records."""

        ...

    async def resolve(
        self,
        tenant_id: str,
        conflict_id: str,
        status: MemoryConflictStatus,
        resolution_decision_id: str,
        resolved_at: datetime,
    ) -> MemoryConflictRecord:
        """Resolve or dismiss through an attributable admission or alias decision."""

        ...

    async def get_resolution_by_idempotency_hash(
        self,
        tenant_id: str,
        idempotency_key_hash: str,
    ) -> MemoryConflictResolutionDecision | None:
        """Read one tenant-scoped immutable conflict decision for safe replay."""

        ...

    async def resolve_with_decision(
        self,
        decision: MemoryConflictResolutionDecision,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> MemoryConflictRecord:
        """Atomically persist a human decision, close the conflict and queue reevaluation."""

        ...

    async def delete_tenant(self, tenant_id: str) -> int:
        """Delete all tenant conflicts without touching original review facts."""

        ...

    async def purge_resolved_before(
        self,
        tenant_id: str,
        older_than: datetime,
    ) -> int:
        """Purge resolved or dismissed conflicts after their retention period."""

        ...


class ReviewerReliabilityRepository(Protocol):
    """Persist versioned reviewer quality signals without granting approval authority."""

    async def get(
        self,
        tenant_id: str,
        reviewer_id: str,
        profile_version: str,
    ) -> ReviewerReliabilityProfile | None:
        """Read one tenant-scoped profile version."""

        ...

    async def save(
        self,
        profile: ReviewerReliabilityProfile,
    ) -> ReviewerReliabilityProfile:
        """Idempotently persist a profile calculated from approved audit facts."""

        ...

    async def delete_tenant(self, tenant_id: str) -> int:
        """Delete every reviewer reliability snapshot owned by a tenant."""

        ...

    async def purge_before(self, tenant_id: str, older_than: datetime) -> int:
        """Purge reviewer snapshots older than the configured retention boundary."""

        ...
