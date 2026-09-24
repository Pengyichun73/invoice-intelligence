"""Stable HTTP schemas for extraction runs, results, and human review."""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from pydantic import (
    JsonValue as PydanticJsonValue,
)

from invoice_intelligence.application.ports.business_persistence import ReviewTaskStatus
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    OCRComparisonOutcome,
    Readability,
)
from invoice_intelligence.domain.field_semantics import (
    FieldBindingDecisionAuthority,
    FieldBindingStatus,
    FieldContextRelation,
)
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.workflow import (
    HumanReviewAction,
    SignalVerdict,
    ValidationRoute,
    WorkflowStatus,
)


class FieldReviewDecisionRequest(BaseModel):
    """One explicit human decision; unchanged fields are not implicit confirmations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str = Field(min_length=1)
    action: HumanReviewAction
    reason: str | None = None
    rejected_value: PydanticJsonValue = None

    @field_validator("field_path")
    @classmethod
    def strip_non_empty_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Review field_path must not be blank")
        return normalized

    @field_validator("reason")
    @classmethod
    def strip_optional_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Review reason must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_action_payload(self) -> "FieldReviewDecisionRequest":
        if self.action in {
            HumanReviewAction.CORRECT,
            HumanReviewAction.CONFIRM_INCORRECT,
        } and self.reason is None:
            raise ValueError("Corrected and rejected decisions require a reason")
        rejected_was_supplied = "rejected_value" in self.model_fields_set
        if (
            self.action is HumanReviewAction.CONFIRM_INCORRECT
            and not rejected_was_supplied
        ):
            raise ValueError("confirm_incorrect requires an explicit rejected_value")
        if (
            self.action is not HumanReviewAction.CONFIRM_INCORRECT
            and rejected_was_supplied
            and self.rejected_value is not None
        ):
            raise ValueError("Only confirm_incorrect may provide rejected_value")
        return self


class FieldBindingReviewDecisionRequest(BaseModel):
    """Explicit human mapping choice; it cannot alter the current invoice value."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    selected_canonical_field_path: str = Field(min_length=1, max_length=512)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("evidence_id", "selected_canonical_field_path", "reason")
    @classmethod
    def strip_binding_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Field binding review values must not be blank")
        return normalized


class HumanCorrectionRequest(BaseModel):
    """Attributable field decisions with an optional corrected invoice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    corrected_invoice: InvoiceExtraction | None = None
    fields: tuple[FieldReviewDecisionRequest, ...] = ()
    field_bindings: tuple[FieldBindingReviewDecisionRequest, ...] = ()
    document_type: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("document_type")
    @classmethod
    def strip_document_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("document_type must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_unique_paths(self) -> "HumanCorrectionRequest":
        if not self.fields and not self.field_bindings:
            raise ValueError("Human review requires a field or binding decision")
        paths = [item.field_path for item in self.fields]
        if len(paths) != len(set(paths)):
            raise ValueError("Review field_path values must be unique")
        if (
            any(item.action is HumanReviewAction.CORRECT for item in self.fields)
            and self.corrected_invoice is None
        ):
            raise ValueError("Correct actions require corrected_invoice")
        if (
            not any(item.action is HumanReviewAction.CORRECT for item in self.fields)
            and self.corrected_invoice is not None
        ):
            raise ValueError("corrected_invoice is only allowed for correct actions")
        evidence_ids = [item.evidence_id for item in self.field_bindings]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Field binding evidence_id values must be unique")
        return self


class FieldEvidenceResponse(BaseModel):
    """Persistable field-level evidence without model reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str
    source: EvidenceSource
    page_number: int | None
    candidate_values: tuple[str, ...]
    readability: Readability
    validation_signals: tuple[str, ...]
    ambiguous: bool = False


class ExtractionAnomalyResponse(BaseModel):
    """Safe extraction anomaly returned to a reviewer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    field_path: str | None
    page_number: int | None


class PageQualityResponse(BaseModel):
    """Deterministic page clarity and normalized resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_number: int
    width: int
    height: int
    clarity_score: float


class OCRFieldObservationResponse(BaseModel):
    """Optional independent OCR candidates used for validation only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str
    page_number: int | None
    candidate_values: tuple[str, ...]
    source_id: str = "legacy"
    provider_name: str = "unknown"
    provider_version: str = "unknown"
    model_version: str = "unknown"
    observed_text: str = ""
    normalized_text: str = ""
    bounding_box: tuple[float, float, float, float] | None = None
    provider_score: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reference: str | None = None
    anomalies: tuple[str, ...] = ()
    candidate_field_paths: tuple[str, ...] = ()
    binding_status: FieldBindingStatus = FieldBindingStatus.ACCEPTED
    binding_reason_codes: tuple[str, ...] = ()


class RawOCRObservationResponse(BaseModel):
    """Unbound OCR line retained as technical evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    provider_name: str
    provider_version: str
    model_version: str
    page_number: int
    observed_text: str
    normalized_text: str
    bounding_box: tuple[float, float, float, float] | None
    provider_score: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reference: str | None = None
    anomalies: tuple[str, ...] = ()


class OCRVisionComparisonResponse(BaseModel):
    """Canonical aggregation without authority to alter InvoiceExtraction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_field_path: str
    vision_candidates: tuple[str, ...]
    ocr_candidates: tuple[str, ...]
    supporting_sources: tuple[str, ...]
    conflicting_sources: tuple[str, ...]
    outcome: OCRComparisonOutcome
    reason_codes: tuple[str, ...]
    review_required: bool


class FieldContextObservationResponse(BaseModel):
    """Nearby text relation used to assess a visual label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    normalized_text: str
    relation: FieldContextRelation
    distance: int | None


class FieldBindingDecisionSummaryResponse(BaseModel):
    """Uncalibrated binding decision summary without vector-search payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    evidence_id: str
    document_type: str
    schema_version: str
    status: FieldBindingStatus
    selected_canonical_field_path: str | None
    reason_codes: tuple[str, ...]
    catalog_version: str
    index_version: str | None
    policy_version: str
    authority: FieldBindingDecisionAuthority
    top1_score: float | None
    top2_score: float | None
    score_margin: float | None
    requires_review: bool


class FieldBindingEvidenceResponse(BaseModel):
    """Current-image label, candidates, and non-authoritative binding result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    document_id: str
    page_number: int
    image_reference: str
    observed_label: str
    normalized_label: str
    nearby_text: tuple[str, ...]
    observed_value_type: str | None
    bounding_box: tuple[int, int, int, int] | None
    context_observations: tuple[FieldContextObservationResponse, ...]
    candidate_field_paths: tuple[str, ...]
    binding_decision: FieldBindingDecisionSummaryResponse | None


class ExtractionResultResponse(BaseModel):
    """Business extraction kept separate from field evidence and anomalies."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invoice: InvoiceExtraction | None
    field_evidence: tuple[FieldEvidenceResponse, ...]
    anomalies: tuple[ExtractionAnomalyResponse, ...]
    page_quality: tuple[PageQualityResponse, ...] = ()
    raw_ocr_observations: tuple[RawOCRObservationResponse, ...] = ()
    ocr_observations: tuple[OCRFieldObservationResponse, ...] = ()
    ocr_comparisons: tuple[OCRVisionComparisonResponse, ...] = ()
    field_binding_evidence: tuple[FieldBindingEvidenceResponse, ...] = ()


class ValidationIssueResponse(BaseModel):
    """Deterministic validation issue that caused review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    field_path: str | None


class ValidationSignalResponse(BaseModel):
    """One independent field-level validation signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str | None
    rule: str
    verdict: SignalVerdict
    score: float
    message: str
    observed_value: PydanticJsonValue
    expected: str | None
    failure_route: ValidationRoute


class FieldDecisionResponse(BaseModel):
    """Composed field-level routing decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str | None
    current_value: PydanticJsonValue
    candidate_values: tuple[str, ...]
    signals: tuple[ValidationSignalResponse, ...]
    score: float
    route: ValidationRoute
    user_action: str | None


class ReviewFieldResponse(BaseModel):
    """One field and the reasons it requires human confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str | None
    current_value: PydanticJsonValue
    candidate_values: tuple[str, ...]
    triggered_rules: tuple[str, ...]
    reasons: tuple[str, ...]
    user_action: str


class ReviewEvidenceSummaryResponse(BaseModel):
    """Bounded OCR/Vision evidence without complete OCR line text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    field_path: str
    source_type: str
    source_id: str
    candidate_values: tuple[str, ...]
    page_number: int | None
    bounding_box: tuple[float, float, float, float] | None
    provider_name: str | None
    provider_version: str | None
    model_version: str | None
    provider_score: float | None = Field(default=None, ge=0.0, le=1.0)
    source_reference: str | None
    comparison_outcome: str | None
    reason_codes: tuple[str, ...]


class ReviewRequestResponse(BaseModel):
    """Pending human-review payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: tuple[ReviewFieldResponse, ...]
    field_bindings: tuple[FieldBindingEvidenceResponse, ...] = ()
    evidence_sources: tuple[ReviewEvidenceSummaryResponse, ...] = ()


class ExtractionRunResponse(BaseModel):
    """Stable business representation of one extraction run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    document_id: str
    status: WorkflowStatus
    validation_route: ValidationRoute | None
    failure_message: str | None
    memory_status: Literal["pending", "failed_retryable", "completed"] | None = None
    memory_trace_id: str | None = None
    memory_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class ExtractionResultEnvelopeResponse(BaseModel):
    """Final result plus stable business identifiers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    document_id: str
    result: ExtractionResultResponse
    created_at: datetime


class ReviewTaskResponse(BaseModel):
    """Persisted human-review task for one extraction run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    status: ReviewTaskStatus
    request: ReviewRequestResponse
    current_invoice: InvoiceExtraction | None
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
