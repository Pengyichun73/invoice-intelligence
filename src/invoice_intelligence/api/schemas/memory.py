"""Stable HTTP contracts for tenant-scoped memory governance."""

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

from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecisionAuthority,
    MemoryAdmissionRecommendation,
    MemoryAdmissionStatus,
    MemoryAssessmentSource,
    MemoryConflictStatus,
)
from invoice_intelligence.domain.evaluation import (
    EvaluationBucketDimension,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationVariant,
)
from invoice_intelligence.domain.examples import ExampleLabelType, IndexProjectionStatus
from invoice_intelligence.domain.field_semantics import (
    FieldAliasStatus,
    FieldContextRelation,
)
from invoice_intelligence.domain.governance import (
    GovernanceAction,
    RetrievalFeedbackLabel,
)


class GovernanceReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("reason must not be blank")
        return normalized


class AdmissionDecisionRequest(GovernanceReasonRequest):
    expected_revision: int = Field(gt=0)


class AdmissionBatchDecisionItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(gt=0)

    @field_validator("admission_id")
    @classmethod
    def normalize_admission_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("admission_id must not be blank")
        return normalized


class AdmissionBatchDecisionRequest(GovernanceReasonRequest):
    items: tuple[AdmissionBatchDecisionItemRequest, ...] = Field(
        min_length=1,
        max_length=100,
    )

    @model_validator(mode="after")
    def require_unique_admissions(self) -> "AdmissionBatchDecisionRequest":
        identifiers = tuple(item.admission_id for item in self.items)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Batch admission identifiers must be unique")
        return self


class FieldAliasDecisionRequest(GovernanceReasonRequest):
    expected_revision: int = Field(gt=0)


class MemoryQualitySignalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    source: MemoryAssessmentSource
    verdict: str
    score: float | None
    message: str
    field_path: str | None
    evidence_references: tuple[str, ...]


class MemoryQualityAssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_id: str
    source: MemoryAssessmentSource
    signals: tuple[MemoryQualitySignalResponse, ...]
    quality_score: float
    recommendation: MemoryAdmissionRecommendation
    reason_codes: tuple[str, ...]
    policy_version: str
    model_version: str | None
    prompt_version: str | None
    advisory_only: bool
    assessed_at: datetime


class MemoryAdmissionDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    previous_status: MemoryAdmissionStatus | None
    status: MemoryAdmissionStatus
    authority: MemoryAdmissionDecisionAuthority
    decided_by: str
    reason: str
    reason_codes: tuple[str, ...]
    assessment_ids: tuple[str, ...]
    conflict_ids: tuple[str, ...]
    policy_version: str
    revision: int
    decided_at: datetime


class ExampleEvidenceReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_reference: str
    image_reference: str | None
    page_number: int | None
    evidence_source: str | None
    candidate_values: tuple[str, ...]
    readability: str | None
    validation_signals: tuple[str, ...]
    ambiguous: bool


class MemoryAdmissionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_id: str
    example_id: str
    document_id: str
    run_id: str
    document_type: str
    field_path: str
    schema_version: str
    catalog_version: str
    model_version: str
    prompt_version: str
    label_type: ExampleLabelType
    model_value: PydanticJsonValue
    reviewed_value: PydanticJsonValue
    correction_reason: str | None
    evidence_reference: ExampleEvidenceReferenceResponse
    source_reviewer_id: str
    is_reviewed: bool
    is_valid: bool
    status: MemoryAdmissionStatus
    current_decision_id: str
    policy_version: str
    revision: int
    created_at: datetime
    updated_at: datetime


class MemoryAdmissionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MemoryAdmissionResponse, ...]
    next_cursor: str | None


class MemoryConflictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict_id: str
    example_ids: tuple[str, ...]
    field_alias_candidate_ids: tuple[str, ...]
    document_type: str
    field_path: str
    schema_version: str
    conflict_type: str
    reason_codes: tuple[str, ...]
    evidence_references: tuple[str, ...]
    candidate_field_paths: tuple[str, ...]
    status: MemoryConflictStatus
    detected_at: datetime
    resolved_at: datetime | None
    resolution_decision_id: str | None


class MemoryConflictResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1, max_length=2000)
    expected_status: MemoryConflictStatus
    selected_canonical_field_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
    )
    resolution_note: str | None = Field(
        default=None,
        min_length=1,
        max_length=2000,
    )

    @field_validator(
        "reason",
        "selected_canonical_field_path",
        "resolution_note",
    )
    @classmethod
    def normalize_conflict_resolution_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Conflict resolution text must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_open_expected_status(self) -> "MemoryConflictResolutionRequest":
        if self.expected_status is not MemoryConflictStatus.OPEN:
            raise ValueError("expected_status must be open")
        return self


class MemoryConflictReevaluationTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_type: str
    target_id: str
    status: str


class MemoryConflictResolutionDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution_decision_id: str
    conflict_id: str
    previous_status: MemoryConflictStatus
    target_status: MemoryConflictStatus
    selected_canonical_field_path: str | None
    reviewer_id: str
    reason: str
    resolution_note: str | None
    policy_version: str
    decided_at: datetime
    reevaluation_targets: tuple[MemoryConflictReevaluationTargetResponse, ...]


class MemoryConflictResolutionResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conflict: MemoryConflictResponse
    decision: MemoryConflictResolutionDecisionResponse
    reevaluation_registered: bool
    audit_id: str | None
    resource_version: str | None
    trace_id: str | None


class MemoryAdmissionDetailResponse(MemoryAdmissionResponse):
    assessments: tuple[MemoryQualityAssessmentResponse, ...]
    decisions: tuple[MemoryAdmissionDecisionResponse, ...]
    open_conflicts: tuple[MemoryConflictResponse, ...]


class MemoryAdmissionDecisionResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission: MemoryAdmissionResponse
    decision: MemoryAdmissionDecisionResponse
    projection_reconciled: bool
    audit_id: str | None
    resource_version: str | None
    trace_id: str | None


class MemoryAdmissionBatchDecisionItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_id: str
    succeeded: bool
    result: MemoryAdmissionDecisionResultResponse | None
    error_type: str | None
    error_message: str | None


class MemoryAdmissionBatchDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_status: MemoryAdmissionStatus
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    items: tuple[MemoryAdmissionBatchDecisionItemResponse, ...]


class FieldContextAnchorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_id: str
    text: str
    normalized_text: str
    relation: FieldContextRelation
    max_distance: int | None
    is_negative: bool


class FieldAliasResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias_id: str
    alias_text: str
    normalized_alias: str
    is_negative: bool
    status: FieldAliasStatus
    is_valid: bool
    context_anchors: tuple[FieldContextAnchorResponse, ...]
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_reason: str | None


class FieldSemanticDefinitionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    document_type: str
    canonical_field_path: str
    display_name: str
    description: str
    value_type: str
    aliases: tuple[FieldAliasResponse, ...]
    negative_aliases: tuple[FieldAliasResponse, ...]
    context_anchors: tuple[FieldContextAnchorResponse, ...]
    catalog_version: str
    is_valid: bool


class FieldAliasSupportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    window_started_at: datetime
    window_ended_at: datetime
    source_count: int
    distinct_documents: int
    distinct_templates: int
    distinct_reviewers: int


class FieldAliasCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias_id: str
    schema_version: str
    document_type: str
    canonical_field_path: str
    alias_text: str
    normalized_alias: str
    policy_version: str
    revision: int = Field(gt=0)
    status: FieldAliasStatus
    canonical_collision: bool
    support: FieldAliasSupportResponse
    source_reviewer_count: int
    conflict_ids: tuple[str, ...]
    submitted_at: datetime
    updated_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_reason: str | None
    promoted_catalog_version: str | None


class FieldSemanticListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    definitions: tuple[FieldSemanticDefinitionResponse, ...]
    alias_candidates: tuple[FieldAliasCandidateResponse, ...]
    next_cursor: str | None


class FieldSemanticConflictListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MemoryConflictResponse, ...]
    next_cursor: str | None


class FieldAliasDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    alias: FieldAliasCandidateResponse
    promoted_catalog_version: str | None
    catalog_activation_required: bool
    audit_id: str | None
    resource_version: str | None
    trace_id: str | None


class IndexRebuildRequest(GovernanceReasonRequest):
    index_version: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(min_length=1, max_length=64)
    dense_model_version: str = Field(min_length=1, max_length=256)
    sparse_model_version: str | None = Field(default=None, min_length=1, max_length=256)
    rerank_model_version: str | None = Field(default=None, min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=256)

    @field_validator(
        "index_version",
        "schema_version",
        "dense_model_version",
        "sparse_model_version",
        "rerank_model_version",
        "prompt_version",
    )
    @classmethod
    def normalize_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Version identifiers must not be blank")
        return normalized


class IndexProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int | None = Field(default=None, ge=1, le=500)


class FieldSemanticIndexRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_version: str = Field(min_length=1, max_length=256)
    schema_version: str = Field(min_length=1, max_length=64)
    catalog_version: str = Field(min_length=1, max_length=128)
    dense_model_version: str = Field(min_length=1, max_length=256)
    sparse_model_version: str | None = Field(default=None, min_length=1, max_length=256)


class MemoryFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str = Field(min_length=1, max_length=64)
    example_id: str = Field(min_length=1, max_length=64)
    label: RetrievalFeedbackLabel
    reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("trace_id", "example_id", "reason")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Feedback identifiers and reason must not be blank")
        return normalized

    @model_validator(mode="after")
    def require_negative_reason(self) -> "MemoryFeedbackRequest":
        if self.label is not RetrievalFeedbackLabel.HELPFUL and self.reason is None:
            raise ValueError("Negative retrieval feedback requires a reason")
        return self


class MemoryExampleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    example_id: str
    source_feedback_id: str
    source_event_id: str | None
    document_id: str
    run_id: str
    document_type: str
    field_path: str
    schema_version: str
    catalog_version: str
    model_version: str
    prompt_version: str
    label_type: ExampleLabelType
    model_value: PydanticJsonValue
    reviewed_value: PydanticJsonValue
    correction_reason: str | None
    vendor_fingerprint: str | None
    template_fingerprint: str | None
    evidence_reference: ExampleEvidenceReferenceResponse
    reviewer_id: str
    is_reviewed: bool
    is_valid: bool
    invalidated_reason: str | None
    invalidated_at: datetime | None
    occurrence_count: int
    created_at: datetime
    last_seen_at: datetime


class MemoryExampleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[MemoryExampleResponse, ...]
    next_cursor: str | None


class MemoryExampleProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_version: str
    projection_status: IndexProjectionStatus
    attempt_count: int = Field(ge=0)
    projection_checksum: str | None
    last_error_code: str | None
    processing_started_at: datetime | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None
    invalidated_at: datetime | None


class MemoryExampleProjectionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    example_id: str
    admission_status: MemoryAdmissionStatus
    eligible_for_long_term_retrieval: bool
    state_source: Literal["postgresql"] = "postgresql"
    milvus_realtime_verified: Literal[False] = False
    items: tuple[MemoryExampleProjectionResponse, ...]
    next_cursor: str | None


class GovernanceOperationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: GovernanceAction
    resource_id: str
    changed_count: int
    audit_id: str
    resource_version: str | None
    trace_id: str | None
    performed_at: datetime


class GovernanceAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    operation: GovernanceAction
    resource_type: str
    resource_id: str
    actor: str
    reason: str
    resource_version: str | None = None
    trace_id: str | None = None
    timestamp: datetime


class GovernanceAuditListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[GovernanceAuditResponse, ...]
    next_cursor: str | None


class IndexProjectionCountsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pending: int
    processing: int
    indexed: int
    failed: int
    invalidated: int
    total: int


class RetrievalMetricSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_count: int
    empty_retrieval_rate: float
    positive_hit_rate: float
    negative_hit_rate: float
    review_required_rate: float | None
    remote_model_error_rate: float
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost: float | None


class MemoryIndexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_version: str
    schema_version: str
    dense_model_version: str
    sparse_model_version: str | None
    rerank_model_version: str | None
    prompt_version: str
    is_active: bool
    is_valid: bool
    invalidated_reason: str | None
    projection_counts: IndexProjectionCountsResponse
    metrics: RetrievalMetricSummaryResponse
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None
    invalidated_at: datetime | None
    audit_id: str | None = None
    resource_version: str | None = None
    trace_id: str | None = None


class IndexProjectionExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claimed: int
    indexed: int
    failed: int
    index: MemoryIndexResponse


class FieldSemanticIndexResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_version: str
    schema_version: str
    catalog_version: str
    dense_model_version: str
    sparse_model_version: str | None
    is_active: bool
    is_valid: bool
    pending_count: int
    processing_count: int
    indexed_count: int
    failed_count: int
    invalidated_count: int
    created_at: datetime
    activated_at: datetime | None
    retired_at: datetime | None
    invalidated_at: datetime | None
    invalidated_reason: str | None


class FieldSemanticProjectionExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claimed: int
    indexed: int
    failed: int
    index: FieldSemanticIndexResponse


class OCRProviderVersionSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    provider_version: str
    model_version: str
    config_version: str
    call_count: int


class OCRMetricsSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_call_count: int
    page_call_count: int
    total_latency_ms: float
    average_call_latency_ms: float
    average_page_latency_ms: float
    success_count: int
    timeout_count: int
    circuit_open_count: int
    schema_error_count: int
    other_error_count: int
    text_box_count: int
    bound_field_count: int
    corroborated_count: int
    conflicting_count: int
    ocr_only_count: int
    vision_only_count: int
    unresolved_count: int
    unavailable_count: int
    empty_ocr_rate: float | None
    conflict_review_required_rate: float | None
    providers: tuple[OCRProviderVersionSummaryResponse, ...]


class MemoryFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str
    trace_id: str
    example_id: str
    label: RetrievalFeedbackLabel
    reviewer_id: str
    reason: str | None
    created_at: datetime
    audit_id: str | None
    governance_trace_id: str | None


class EvaluationBindingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    index_version: str
    model_version: str
    prompt_version: str
    retrieval_policy_version: str
    threshold_version: str
    catalog_version: str | None
    admission_policy_version: str | None
    field_binding_policy_version: str | None


class RetrievalMetricResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    k: int
    recall_at_k: float | None
    hit_rate_at_k: float | None
    mrr: float | None
    ndcg_at_k: float | None
    positive_negative_separation: float | None
    empty_retrieval_rate: float
    evaluation_case_count: int
    expected_example_case_count: int
    expected_example_count: int
    relevant_hit_count: int
    empty_retrieval_count: int
    separation_case_count: int


class ExtractionMetricResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    field_accuracy: float | None
    missing_recognition_accuracy: float
    candidate_hit_rate: float | None
    erroneous_auto_filled_value_count: int
    historical_override_violation_count: int
    review_required_precision: float | None
    review_required_recall: float | None
    evaluation_case_count: int
    field_accuracy_denominator: int
    missing_recognition_correct_count: int
    candidate_case_count: int
    candidate_hit_count: int
    review_true_positive_count: int
    review_false_positive_count: int
    review_false_negative_count: int


class TrustedMemoryFieldMetricResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    k: int
    memory_approval_precision: float | None
    harmful_memory_admission_rate: float | None
    quarantine_rate: float | None
    reviewer_disagreement_rate: float | None
    alias_binding_accuracy: float | None
    top_k_field_recall: float | None
    field_binding_ambiguity_rate: float | None
    wrong_field_auto_fill_count: int
    memory_helpfulness_rate: float | None
    misleading_retrieval_rate: float | None
    evaluation_case_count: int
    memory_admission_case_count: int
    approved_prediction_count: int
    correct_approved_count: int
    harmful_ground_truth_count: int
    harmful_admitted_count: int
    quarantined_prediction_count: int
    multi_reviewer_case_count: int
    reviewer_disagreement_count: int
    alias_binding_case_count: int
    correct_alias_binding_count: int
    field_binding_case_count: int
    expected_field_path_count: int
    recalled_field_path_count: int
    ambiguous_prediction_count: int
    helpful_opportunity_count: int
    helpful_hit_count: int
    memory_effect_case_count: int
    misleading_hit_count: int


class EvaluationBucketResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    dimension: EvaluationBucketDimension
    value: str


class EvaluationBucketResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    bucket: EvaluationBucketResponse
    retrieval_metrics: tuple[RetrievalMetricResponse, ...]
    extraction_metric: ExtractionMetricResponse
    trusted_memory_field_metrics: tuple[TrustedMemoryFieldMetricResponse, ...]


class EvaluationVariantResultResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    variant: EvaluationVariant
    overall: EvaluationBucketResultResponse
    buckets: tuple[EvaluationBucketResultResponse, ...]


class PromotionCandidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, from_attributes=True)

    candidate_id: str
    evaluation_run_id: str
    baseline_variant: EvaluationVariant
    candidate_variant: EvaluationVariant
    metric_deltas: dict[str, float]
    rationale_codes: tuple[str, ...]
    created_at: datetime
    status: str
    requires_human_approval: bool
    may_modify_production: bool


class EvaluationRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_run_id: str
    dataset_id: str
    dataset_version: str
    schema_version: str
    suite: EvaluationSuite
    bindings: EvaluationBindingsResponse
    variants: tuple[EvaluationVariant, ...]
    status: EvaluationRunStatus
    results: tuple[EvaluationVariantResultResponse, ...]
    promotion_candidates: tuple[PromotionCandidateResponse, ...]
    leakage_check_passed: bool
    report_schema_version: str
    artifact_references: tuple[str, ...]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    failure_code: str | None
