"""Framework-independent contracts for controlled training-data preparation."""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from invoice_intelligence.domain.examples import RetrievalScore
from invoice_intelligence.domain.workflow import JsonValue


class MiningSignalType(StrEnum):
    """Auditable reasons why a reviewed example may be a hard negative."""

    CONFIRMED_INCORRECT = "confirmed_incorrect"
    HIGH_RECALL_HUMAN_REJECTED = "high_recall_human_rejected"
    HIGH_RERANK_HUMAN_REJECTED = "high_rerank_human_rejected"
    VENDOR_TEMPLATE_CONFUSION = "vendor_template_confusion"
    SAME_DOCUMENT_FIELD_CONFUSION = "same_document_field_confusion"
    LLM_PROPOSED = "llm_proposed"


class CandidateProposalSource(StrEnum):
    """Candidate provenance; only human sources may bypass pending review."""

    REVIEWED_LABEL = "reviewed_label"
    HUMAN_RETRIEVAL_JUDGMENT = "human_retrieval_judgment"
    DETERMINISTIC_RULE = "deterministic_rule"
    LLM_PROPOSAL = "llm_proposal"


class CandidateReviewStatus(StrEnum):
    """Human gate for one mined negative pair."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TrainingSplit(StrEnum):
    """Document/template-grouped dataset splits."""

    TRAIN = "train"
    VALIDATION = "validation"
    EVALUATION = "evaluation"


class TrainingDatasetStatus(StrEnum):
    """Persisted lifecycle for immutable dataset contents and derived artifacts."""

    BUILDING = "building"
    EXPORTED = "exported"
    FAILED = "failed"


class TrainingTargetType(StrEnum):
    """Ordered model capabilities eligible for controlled offline training."""

    RERANKER = "reranker"
    EMBEDDING = "embedding"
    VISION_GENERATION = "vision_generation"


TRAINING_TARGET_PRIORITY: tuple[TrainingTargetType, ...] = (
    TrainingTargetType.RERANKER,
    TrainingTargetType.EMBEDDING,
    TrainingTargetType.VISION_GENERATION,
)


class TrainingRunStatus(StrEnum):
    """Monotonic lifecycle owned by an offline worker, never FastAPI."""

    CREATED = "created"
    EXPORT_READY = "export_ready"
    SUBMITTED = "submitted"
    QUEUED = "queued"
    RUNNING = "running"
    CANCELING = "canceling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    UNSUPPORTED = "unsupported"


class ModelLifecycleStage(StrEnum):
    """Mandatory promotion order for a registered model artifact."""

    REGISTERED = "registered"
    OFFLINE_EVALUATION = "offline_evaluation"
    SHADOW = "shadow"
    CANARY = "canary"
    PRODUCTION = "production"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


class PromotionCandidateStatus(StrEnum):
    """Independent promotion state; ModelArtifact.stage remains backward compatible."""

    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    ROLLBACK = "rollback"
    REJECTED = "rejected"


class ModelEvaluationStatus(StrEnum):
    """Outcome of a version-bound model gate evaluation."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class DeploymentStatus(StrEnum):
    """Deployment control-plane state independent from model training state."""

    REQUESTED = "requested"
    ACTIVE = "active"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class PromotionDecisionType(StrEnum):
    """Auditable outcome of one promotion or rollback gate."""

    APPROVED = "approved"
    REJECTED = "rejected"
    ROLLBACK_REQUIRED = "rollback_required"


@dataclass(frozen=True, slots=True)
class PromotionCandidateRecord:
    """Version-bound candidate owned by PostgreSQL and changed with revision/CAS."""

    candidate_id: str
    tenant_id: str
    dataset_version: str
    evaluation_run_id: str
    model_version: str
    prompt_version: str
    schema_version: str
    index_version: str
    threshold_version: str
    status: PromotionCandidateStatus
    revision: int
    metric_values: dict[str, float]
    hard_failure_code: str | None
    compatibility_errors: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    approved_by: str | None = None
    rejection_reason: str | None = None
    artifact_id: str | None = None
    model_evaluation_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id), ("tenant_id", self.tenant_id),
            ("dataset_version", self.dataset_version),
            ("evaluation_run_id", self.evaluation_run_id),
            ("model_version", self.model_version), ("prompt_version", self.prompt_version),
            ("schema_version", self.schema_version), ("index_version", self.index_version),
            ("threshold_version", self.threshold_version),
        ):
            _require_text(name, value)
        if self.revision < 1:
            raise ValueError("Promotion candidate revision must be positive")
        if (self.artifact_id is None) != (self.model_evaluation_id is None):
            raise ValueError("Promotion evidence references must be present together")
        if self.artifact_id is not None:
            _require_text("artifact_id", self.artifact_id)
            _require_text("model_evaluation_id", self.model_evaluation_id or "")
        if any(not math.isfinite(v) for v in self.metric_values.values()):
            raise ValueError("Promotion candidate metrics must be finite")
        if self.status is PromotionCandidateStatus.ACTIVE and self.approved_by is None:
            raise ValueError("Active promotion candidates require human approval")
        if self.status is PromotionCandidateStatus.REJECTED and not self.rejection_reason:
            raise ValueError("Rejected promotion candidates require a reason")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Promotion candidate timestamps must be timezone-aware")


class MetricDirection(StrEnum):
    """Direction used to calculate regression without treating scores as probabilities."""

    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


@dataclass(frozen=True, slots=True)
class HumanRetrievalJudgment:
    """Explicit human rejection of one retrieved example for a reviewed query case."""

    judgment_id: str
    tenant_id: str
    query_example_id: str
    retrieved_example_id: str
    schema_version: str
    scores: RetrievalScore
    rank: int
    reviewer_id: str
    rejection_reason: str
    created_at: datetime
    is_relevant: Literal[False] = False
    is_valid: bool = True

    def __post_init__(self) -> None:
        for name, value in (
            ("judgment_id", self.judgment_id),
            ("tenant_id", self.tenant_id),
            ("query_example_id", self.query_example_id),
            ("retrieved_example_id", self.retrieved_example_id),
            ("schema_version", self.schema_version),
            ("reviewer_id", self.reviewer_id),
            ("rejection_reason", self.rejection_reason),
        ):
            _require_text(name, value)
        if self.query_example_id == self.retrieved_example_id:
            raise ValueError("A retrieval judgment cannot reject its own query example")
        if self.rank <= 0:
            raise ValueError("Retrieval judgment rank must be greater than zero")
        if self.is_relevant is not False:
            raise ValueError("Hard-negative judgments must be explicit human rejections")
        if self.created_at.tzinfo is None:
            raise ValueError("Retrieval judgment created_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HardNegativeReview:
    """Attributable human decision for a candidate proposed without a final label."""

    review_id: str
    candidate_id: str
    tenant_id: str
    decision: CandidateReviewStatus
    reviewer_id: str
    reason: str
    reviewed_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("review_id", self.review_id),
            ("candidate_id", self.candidate_id),
            ("tenant_id", self.tenant_id),
            ("reviewer_id", self.reviewer_id),
            ("reason", self.reason),
        ):
            _require_text(name, value)
        if self.decision not in {
            CandidateReviewStatus.APPROVED,
            CandidateReviewStatus.REJECTED,
        }:
            raise ValueError("A hard-negative review must approve or reject the candidate")
        if self.reviewed_at.tzinfo is None:
            raise ValueError("Hard-negative review reviewed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class HardNegativeCandidate:
    """A replay-safe positive/negative pairing with explicit review provenance."""

    candidate_id: str
    tenant_id: str
    schema_version: str
    positive_example_id: str
    negative_example_id: str
    signal_types: tuple[MiningSignalType, ...]
    proposal_source: CandidateProposalSource
    rationale: str
    fingerprint: str
    status: CandidateReviewStatus
    source_judgment_id: str | None
    review: HardNegativeReview | None
    is_valid: bool
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("candidate_id", self.candidate_id),
            ("tenant_id", self.tenant_id),
            ("schema_version", self.schema_version),
            ("positive_example_id", self.positive_example_id),
            ("negative_example_id", self.negative_example_id),
            ("rationale", self.rationale),
            ("fingerprint", self.fingerprint),
        ):
            _require_text(name, value)
        if self.positive_example_id == self.negative_example_id:
            raise ValueError("Positive and hard-negative examples must be different")
        if not self.signal_types:
            raise ValueError("A hard-negative candidate requires at least one mining signal")
        if len(self.signal_types) != len(set(self.signal_types)):
            raise ValueError("Hard-negative mining signals must be unique")
        if self.source_judgment_id is not None:
            _require_text("source_judgment_id", self.source_judgment_id)
        if self.proposal_source is CandidateProposalSource.HUMAN_RETRIEVAL_JUDGMENT:
            if self.source_judgment_id is None:
                raise ValueError("Human retrieval candidates require their source judgment")
        elif self.source_judgment_id is not None:
            raise ValueError("Only retrieval-judgment candidates may bind a judgment")
        if self.proposal_source is CandidateProposalSource.LLM_PROPOSAL:
            if self.status is not CandidateReviewStatus.PENDING and self.review is None:
                raise ValueError("LLM proposals cannot approve or reject themselves")
        if self.status is CandidateReviewStatus.PENDING and self.review is not None:
            raise ValueError("Pending candidates cannot carry a final human review")
        if self.status is not CandidateReviewStatus.PENDING:
            if self.review is None:
                raise ValueError("Final candidate status requires a human review")
            if self.review.candidate_id != self.candidate_id:
                raise ValueError("Candidate review references a different candidate")
            if self.review.tenant_id != self.tenant_id:
                raise ValueError("Candidate review crosses a tenant boundary")
            if self.review.decision is not self.status:
                raise ValueError("Candidate status must match its human review")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Hard-negative timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Hard-negative updated_at cannot precede created_at")


@dataclass(frozen=True, slots=True)
class HardNegativeProposal:
    """Non-authoritative pair proposed by a rule or model for later human review."""

    positive_example_id: str
    negative_example_id: str
    signal_type: MiningSignalType
    rationale: str
    proposal_source: Literal[
        CandidateProposalSource.DETERMINISTIC_RULE,
        CandidateProposalSource.LLM_PROPOSAL,
    ]

    def __post_init__(self) -> None:
        _require_text("positive_example_id", self.positive_example_id)
        _require_text("negative_example_id", self.negative_example_id)
        _require_text("rationale", self.rationale)
        if self.positive_example_id == self.negative_example_id:
            raise ValueError("A proposal cannot use the same positive and negative example")
        if self.proposal_source not in {
            CandidateProposalSource.DETERMINISTIC_RULE,
            CandidateProposalSource.LLM_PROPOSAL,
        }:
            raise ValueError("Only rules or models may create unreviewed proposals")


@dataclass(frozen=True, slots=True)
class TrainingRecordScope:
    """Export-safe scope; tenant_scope is opaque and never a raw tenant identifier."""

    tenant_scope: str
    document_type: str
    field_path: str
    schema_version: str
    vendor_fingerprint: str | None
    template_cluster: str | None

    def __post_init__(self) -> None:
        for name, value in (
            ("tenant_scope", self.tenant_scope),
            ("document_type", self.document_type),
            ("field_path", self.field_path),
            ("schema_version", self.schema_version),
        ):
            _require_text(name, value)
        for name, value in (
            ("vendor_fingerprint", self.vendor_fingerprint),
            ("template_cluster", self.template_cluster),
        ):
            if value is not None:
                _require_text(name, value)


@dataclass(frozen=True, slots=True)
class TrainingRecord:
    """Exact provider-neutral JSONL contract requested for later training."""

    query: str
    positive: str
    hard_negatives: tuple[str, ...]
    scope: TrainingRecordScope
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text("query", self.query)
        _require_text("positive", self.positive)
        if not self.hard_negatives:
            raise ValueError("A training record requires at least one hard negative")
        if any(not value.strip() for value in self.hard_negatives):
            raise ValueError("Hard-negative text must not be blank")
        if len(self.hard_negatives) != len(set(self.hard_negatives)):
            raise ValueError("Hard-negative text must be unique")
        if self.positive in self.hard_negatives:
            raise ValueError("A redacted positive cannot also be a hard negative")
        if not self.source_event_ids:
            raise ValueError("A training record requires traceable source event identifiers")
        if any(not value.strip() for value in self.source_event_ids):
            raise ValueError("Source event identifiers must not be blank")
        if len(self.source_event_ids) != len(set(self.source_event_ids)):
            raise ValueError("Source event identifiers must be unique")


@dataclass(frozen=True, slots=True)
class TrainingDatasetRecord:
    """PostgreSQL audit wrapper around the exact exported training contract."""

    record_id: str
    dataset_id: str
    dataset_version: str
    source_tenant_id: str
    source_document_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    group_fingerprint: str
    split: TrainingSplit
    record: TrainingRecord
    fingerprint: str

    def __post_init__(self) -> None:
        for name, value in (
            ("record_id", self.record_id),
            ("dataset_id", self.dataset_id),
            ("dataset_version", self.dataset_version),
            ("source_tenant_id", self.source_tenant_id),
            ("group_fingerprint", self.group_fingerprint),
            ("fingerprint", self.fingerprint),
        ):
            _require_text(name, value)
        for name, values in (
            ("source_document_ids", self.source_document_ids),
            ("candidate_ids", self.candidate_ids),
        ):
            if not values or any(not value.strip() for value in values):
                raise ValueError(f"{name} must contain non-blank values")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must be unique")


@dataclass(frozen=True, slots=True)
class TemplateClusterAssignment:
    """Explicit cross-tenant near-duplicate grouping supplied by data governance."""

    tenant_id: str
    template_fingerprint: str
    cluster_id: str

    def __post_init__(self) -> None:
        _require_text("tenant_id", self.tenant_id)
        _require_text("template_fingerprint", self.template_fingerprint)
        _require_text("cluster_id", self.cluster_id)


@dataclass(frozen=True, slots=True)
class CrossTenantTrainingAuthorization:
    """Time-bounded approval required before any cross-tenant export."""

    authorization_id: str
    tenant_ids: tuple[str, ...]
    opaque_cohort_id: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    template_clusters: tuple[TemplateClusterAssignment, ...]
    irreversible_redaction_required: Literal[True] = True

    def __post_init__(self) -> None:
        for name, value in (
            ("authorization_id", self.authorization_id),
            ("opaque_cohort_id", self.opaque_cohort_id),
            ("approved_by", self.approved_by),
        ):
            _require_text(name, value)
        if len(self.tenant_ids) < 2:
            raise ValueError("Cross-tenant authorization requires at least two tenants")
        if any(not value.strip() for value in self.tenant_ids):
            raise ValueError("Authorized tenant identifiers must not be blank")
        if len(self.tenant_ids) != len(set(self.tenant_ids)):
            raise ValueError("Authorized tenant identifiers must be unique")
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Cross-tenant authorization timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("Cross-tenant authorization must expire after approval")
        keys = tuple(
            (item.tenant_id, item.template_fingerprint) for item in self.template_clusters
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Cross-tenant template assignments must be unique")
        if any(item.tenant_id not in self.tenant_ids for item in self.template_clusters):
            raise ValueError("Template assignment contains an unauthorized tenant")


@dataclass(frozen=True, slots=True)
class TrainingDatasetVersion:
    """Immutable generation bindings plus mutable export lifecycle."""

    dataset_id: str
    version: str
    tenant_scope: str
    source_tenant_ids: tuple[str, ...]
    schema_version: str
    generation_rule_version: str
    redaction_policy_version: str
    split_rule_version: str
    split_salt_version: str
    created_by: str
    created_at: datetime
    cross_tenant: bool
    authorization_id: str | None
    status: TrainingDatasetStatus
    record_count: int
    fingerprint: str
    artifact_references: tuple[str, ...] = ()
    completed_at: datetime | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("dataset_id", self.dataset_id),
            ("version", self.version),
            ("tenant_scope", self.tenant_scope),
            ("schema_version", self.schema_version),
            ("generation_rule_version", self.generation_rule_version),
            ("redaction_policy_version", self.redaction_policy_version),
            ("split_rule_version", self.split_rule_version),
            ("split_salt_version", self.split_salt_version),
            ("created_by", self.created_by),
            ("fingerprint", self.fingerprint),
        ):
            _require_text(name, value)
        if not self.source_tenant_ids:
            raise ValueError("A training dataset requires source tenants")
        if len(self.source_tenant_ids) != len(set(self.source_tenant_ids)):
            raise ValueError("Training dataset source tenants must be unique")
        if any(not value.strip() for value in self.source_tenant_ids):
            raise ValueError("Training dataset source tenants must not be blank")
        if self.cross_tenant != (len(self.source_tenant_ids) > 1):
            raise ValueError("cross_tenant must match the source tenant count")
        if self.cross_tenant and self.authorization_id is None:
            raise ValueError("Cross-tenant datasets require an authorization")
        if not self.cross_tenant and self.authorization_id is not None:
            raise ValueError("Single-tenant datasets cannot bind cross-tenant authorization")
        if self.record_count <= 0:
            raise ValueError("A training dataset requires at least one record")
        if self.created_at.tzinfo is None:
            raise ValueError("Training dataset created_at must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("Training dataset completed_at must be timezone-aware")
        if self.status is TrainingDatasetStatus.BUILDING:
            if self.artifact_references or self.completed_at is not None or self.failure_code:
                raise ValueError("Building datasets cannot contain a terminal outcome")
        elif self.status is TrainingDatasetStatus.EXPORTED:
            if not self.artifact_references or self.completed_at is None or self.failure_code:
                raise ValueError("Exported datasets require artifacts and completion time")
        elif self.completed_at is None or not self.failure_code:
            raise ValueError("Failed datasets require completion time and a safe failure code")


@dataclass(frozen=True, slots=True)
class TrainingHyperparameter:
    """One provider-neutral, versioned hyperparameter value."""

    name: str
    value: JsonValue

    def __post_init__(self) -> None:
        _require_text("Training hyperparameter name", self.name)


@dataclass(frozen=True, slots=True)
class ProviderTrainingCapabilities:
    """Capabilities discovered from one provider without implying unsupported training."""

    provider: str
    capability_version: str
    supported_targets: tuple[TrainingTargetType, ...]
    supports_submission: bool
    discovered_at: datetime
    documentation_reference: str | None = None

    def __post_init__(self) -> None:
        _require_text("Training provider", self.provider)
        _require_text("Training capability version", self.capability_version)
        if len(self.supported_targets) != len(set(self.supported_targets)):
            raise ValueError("Supported training targets must be unique")
        if self.supports_submission != bool(self.supported_targets):
            raise ValueError("Submission support must match the declared training targets")
        if self.discovered_at.tzinfo is None:
            raise ValueError("Capability discovery time must be timezone-aware")
        if self.documentation_reference is not None:
            _require_text("Training documentation reference", self.documentation_reference)


@dataclass(frozen=True, slots=True)
class RemoteTrainingJob:
    """Provider-neutral snapshot of one asynchronous remote training job."""

    provider_job_id: str
    status: TrainingRunStatus
    updated_at: datetime
    artifact_reference: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _require_text("Remote training job identifier", self.provider_job_id)
        allowed = {
            TrainingRunStatus.SUBMITTED,
            TrainingRunStatus.QUEUED,
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.CANCELING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
        }
        if self.status not in allowed:
            raise ValueError("Remote training job has an invalid provider status")
        if self.updated_at.tzinfo is None:
            raise ValueError("Remote training update time must be timezone-aware")
        if self.status is TrainingRunStatus.SUCCEEDED:
            if self.artifact_reference is None or self.failure_code is not None:
                raise ValueError("Successful remote jobs require only an artifact reference")
        elif self.artifact_reference is not None:
            raise ValueError("Only successful remote jobs may expose an artifact reference")
        if self.status is TrainingRunStatus.FAILED and not self.failure_code:
            raise ValueError("Failed remote jobs require a safe failure code")
        if self.failure_code is not None:
            _require_text("Remote training failure code", self.failure_code)


@dataclass(frozen=True, slots=True)
class TrainingRun:
    """Version-bound offline orchestration record isolated from online inference."""

    training_run_id: str
    tenant_id: str
    target_type: TrainingTargetType
    provider: str
    base_model: str
    base_model_version: str
    candidate_model_version_id: str
    candidate_model_version: str
    training_dataset_tenant_scope: str
    training_dataset_id: str
    training_dataset_version: str
    validation_dataset_id: str
    validation_dataset_version: str
    evaluation_dataset_id: str
    evaluation_dataset_version: str
    schema_version: str
    hyperparameters: tuple[TrainingHyperparameter, ...]
    code_version: str
    training_artifact_references: tuple[str, ...]
    status: TrainingRunStatus
    created_at: datetime
    remote_job_id: str | None = None
    provider_artifact_reference: str | None = None
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("Training run identifier", self.training_run_id),
            ("Training tenant identifier", self.tenant_id),
            ("Training provider", self.provider),
            ("Training base model", self.base_model),
            ("Training base model version", self.base_model_version),
            ("Candidate model version identifier", self.candidate_model_version_id),
            ("Candidate model version", self.candidate_model_version),
            ("Training dataset tenant scope", self.training_dataset_tenant_scope),
            ("Training dataset identifier", self.training_dataset_id),
            ("Training dataset version", self.training_dataset_version),
            ("Validation dataset identifier", self.validation_dataset_id),
            ("Validation dataset version", self.validation_dataset_version),
            ("Evaluation dataset identifier", self.evaluation_dataset_id),
            ("Evaluation dataset version", self.evaluation_dataset_version),
            ("Training Schema version", self.schema_version),
            ("Training code version", self.code_version),
        ):
            _require_text(name, value)
        names = tuple(item.name for item in self.hyperparameters)
        if len(names) != len(set(names)):
            raise ValueError("Training hyperparameter names must be unique")
        if any(not item.strip() for item in self.training_artifact_references):
            raise ValueError("Training artifact references must not be blank")
        if len(self.training_artifact_references) != len(
            set(self.training_artifact_references)
        ):
            raise ValueError("Training artifact references must be unique")
        if self.created_at.tzinfo is None:
            raise ValueError("Training run created_at must be timezone-aware")
        for name, value in (
            ("submitted_at", self.submitted_at),
            ("started_at", self.started_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"Training run {name} must be timezone-aware")
        remote_states = {
            TrainingRunStatus.SUBMITTED,
            TrainingRunStatus.QUEUED,
            TrainingRunStatus.RUNNING,
            TrainingRunStatus.CANCELING,
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
        }
        if self.status in remote_states and self.remote_job_id is None:
            raise ValueError("Remote training states require a provider job identifier")
        if self.remote_job_id is not None:
            _require_text("Remote training job identifier", self.remote_job_id)
        if self.status in remote_states and self.submitted_at is None:
            raise ValueError("Remote training states require submitted_at")
        if self.status is TrainingRunStatus.RUNNING and self.started_at is None:
            raise ValueError("Running training requires started_at")
        terminal = {
            TrainingRunStatus.SUCCEEDED,
            TrainingRunStatus.FAILED,
            TrainingRunStatus.CANCELED,
            TrainingRunStatus.UNSUPPORTED,
        }
        if self.status in terminal and self.completed_at is None:
            raise ValueError("Terminal training states require completed_at")
        if self.status is TrainingRunStatus.SUCCEEDED:
            if self.provider_artifact_reference is None or self.failure_code is not None:
                raise ValueError("Successful training requires only a provider artifact")
        elif self.provider_artifact_reference is not None:
            raise ValueError("Only successful training may bind a provider artifact")
        if self.status in {TrainingRunStatus.FAILED, TrainingRunStatus.UNSUPPORTED}:
            if self.failure_code is None:
                raise ValueError("Failed or unsupported training requires a failure code")
        elif self.failure_code is not None:
            raise ValueError("Only failed or unsupported training may have a failure code")
        if self.status in {TrainingRunStatus.CREATED, TrainingRunStatus.EXPORT_READY}:
            if any(
                value is not None
                for value in (
                    self.remote_job_id,
                    self.provider_artifact_reference,
                    self.submitted_at,
                    self.started_at,
                    self.completed_at,
                )
            ):
                raise ValueError("Unsubmitted training cannot contain remote execution state")


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Registered candidate model; provider artifacts never become active implicitly."""

    artifact_id: str
    tenant_id: str
    training_run_id: str
    model_version_id: str
    model_version: str
    target_type: TrainingTargetType
    provider: str
    provider_artifact_reference: str
    base_model: str
    base_model_version: str
    training_dataset_id: str
    training_dataset_version: str
    code_version: str
    stage: ModelLifecycleStage
    created_at: datetime
    is_valid: bool = True
    invalidated_reason: str | None = None
    invalidated_at: datetime | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("Model artifact identifier", self.artifact_id),
            ("Model artifact tenant", self.tenant_id),
            ("Model artifact training run", self.training_run_id),
            ("Model version identifier", self.model_version_id),
            ("Model version", self.model_version),
            ("Model provider", self.provider),
            ("Provider artifact reference", self.provider_artifact_reference),
            ("Model base model", self.base_model),
            ("Model base model version", self.base_model_version),
            ("Model training dataset", self.training_dataset_id),
            ("Model training dataset version", self.training_dataset_version),
            ("Model code version", self.code_version),
        ):
            _require_text(name, value)
        if self.created_at.tzinfo is None:
            raise ValueError("Model artifact created_at must be timezone-aware")
        if self.is_valid:
            if self.invalidated_reason is not None or self.invalidated_at is not None:
                raise ValueError("Valid model artifacts cannot contain invalidation state")
        else:
            if not self.invalidated_reason or self.invalidated_at is None:
                raise ValueError("Invalid model artifacts require reason and timestamp")
            _require_text("Model invalidation reason", self.invalidated_reason)
            if self.invalidated_at.tzinfo is None:
                raise ValueError("Model invalidated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ModelMetricComparison:
    """Candidate/baseline comparison used by a deterministic promotion gate."""

    metric_name: str
    baseline_value: float
    candidate_value: float
    direction: MetricDirection
    maximum_regression: float

    def __post_init__(self) -> None:
        _require_text("Model metric name", self.metric_name)
        for name, value in (
            ("baseline_value", self.baseline_value),
            ("candidate_value", self.candidate_value),
            ("maximum_regression", self.maximum_regression),
        ):
            if not math.isfinite(value):
                raise ValueError(f"Model metric {name} must be finite")
        if self.maximum_regression < 0.0:
            raise ValueError("Maximum metric regression must not be negative")

    @property
    def regression(self) -> float:
        if self.direction is MetricDirection.HIGHER_IS_BETTER:
            return self.baseline_value - self.candidate_value
        return self.candidate_value - self.baseline_value

    @property
    def passed(self) -> bool:
        return self.regression <= self.maximum_regression


@dataclass(frozen=True, slots=True)
class ModelEvaluation:
    """Offline, shadow, or canary evidence bound to exact production inputs."""

    model_evaluation_id: str
    tenant_id: str
    artifact_id: str
    stage: ModelLifecycleStage
    evaluation_run_id: str
    dataset_id: str
    dataset_version: str
    schema_version: str
    baseline_model_version_id: str
    candidate_model_version_id: str
    prompt_version: str
    threshold_version: str
    comparisons: tuple[ModelMetricComparison, ...]
    status: ModelEvaluationStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("Model evaluation identifier", self.model_evaluation_id),
            ("Model evaluation tenant", self.tenant_id),
            ("Model evaluation artifact", self.artifact_id),
            ("Model evaluation run", self.evaluation_run_id),
            ("Model evaluation dataset", self.dataset_id),
            ("Model evaluation dataset version", self.dataset_version),
            ("Model evaluation Schema version", self.schema_version),
            ("Baseline model version identifier", self.baseline_model_version_id),
            ("Candidate model version identifier", self.candidate_model_version_id),
            ("Model evaluation Prompt version", self.prompt_version),
            ("Model evaluation threshold version", self.threshold_version),
        ):
            _require_text(name, value)
        if self.stage not in {
            ModelLifecycleStage.OFFLINE_EVALUATION,
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
            ModelLifecycleStage.PRODUCTION,
        }:
            raise ValueError(
                "Model evaluation stage must be offline, shadow, canary, or production"
            )
        names = tuple(item.metric_name for item in self.comparisons)
        if not names or len(names) != len(set(names)):
            raise ValueError("Model evaluation metric names must be non-empty and unique")
        if self.created_at.tzinfo is None:
            raise ValueError("Model evaluation created_at must be timezone-aware")
        for name, value in (("started_at", self.started_at), ("completed_at", self.completed_at)):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"Model evaluation {name} must be timezone-aware")
        if self.status is ModelEvaluationStatus.PENDING:
            if self.started_at is not None or self.completed_at is not None:
                raise ValueError("Pending model evaluation cannot contain execution timestamps")
        elif self.status is ModelEvaluationStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("Running model evaluation timestamps are inconsistent")
        else:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("Terminal model evaluation requires execution timestamps")
        if self.status is ModelEvaluationStatus.PASSED:
            if any(not item.passed for item in self.comparisons):
                raise ValueError("Passed evaluation contains a regressed gate metric")
            if self.failure_code is not None:
                raise ValueError("Passed model evaluation cannot contain a failure code")
        elif self.status is ModelEvaluationStatus.FAILED:
            if self.failure_code is None:
                raise ValueError("Failed model evaluation requires a failure code")
        elif self.failure_code is not None:
            raise ValueError("Only failed model evaluations may have a failure code")


@dataclass(frozen=True, slots=True)
class ModelDeployment:
    """One idempotent deployment control-plane operation."""

    deployment_id: str
    tenant_id: str
    artifact_id: str
    model_version_id: str
    target_type: TrainingTargetType
    stage: ModelLifecycleStage
    status: DeploymentStatus
    traffic_percentage: float
    requested_by: str
    deployment_reference: str
    created_at: datetime
    previous_deployment_id: str | None = None
    activated_at: datetime | None = None
    completed_at: datetime | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("Model deployment identifier", self.deployment_id),
            ("Model deployment tenant", self.tenant_id),
            ("Model deployment artifact", self.artifact_id),
            ("Model deployment version", self.model_version_id),
            ("Model deployment requester", self.requested_by),
            ("Model deployment reference", self.deployment_reference),
        ):
            _require_text(name, value)
        if self.stage not in {
            ModelLifecycleStage.SHADOW,
            ModelLifecycleStage.CANARY,
            ModelLifecycleStage.PRODUCTION,
        }:
            raise ValueError("Deployments are limited to shadow, canary, and production")
        if (
            not math.isfinite(self.traffic_percentage)
            or not 0.0 <= self.traffic_percentage <= 100.0
        ):
            raise ValueError("Deployment traffic percentage must be between 0 and 100")
        if self.stage is ModelLifecycleStage.SHADOW and self.traffic_percentage != 0.0:
            raise ValueError("Shadow deployment cannot receive production traffic")
        if self.stage is ModelLifecycleStage.CANARY and not 0.0 < self.traffic_percentage < 100.0:
            raise ValueError("Canary deployment requires partial traffic")
        if self.stage is ModelLifecycleStage.PRODUCTION and self.traffic_percentage != 100.0:
            raise ValueError("Production deployment requires all configured traffic")
        if self.previous_deployment_id is not None:
            _require_text("Previous deployment identifier", self.previous_deployment_id)
            if self.previous_deployment_id == self.deployment_id:
                raise ValueError("Deployment cannot reference itself as previous")
        if self.created_at.tzinfo is None:
            raise ValueError("Model deployment created_at must be timezone-aware")
        for name, value in (
            ("activated_at", self.activated_at),
            ("completed_at", self.completed_at),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"Model deployment {name} must be timezone-aware")
        if self.status is DeploymentStatus.REQUESTED:
            if self.activated_at is not None or self.completed_at is not None:
                raise ValueError("Requested deployment cannot contain terminal timestamps")
        elif self.status is DeploymentStatus.ACTIVE:
            if self.activated_at is None or self.completed_at is not None:
                raise ValueError("Active deployment timestamps are inconsistent")
        else:
            if self.completed_at is None:
                raise ValueError("Terminal deployment state requires completed_at")
        if self.status is DeploymentStatus.FAILED:
            if self.failure_code is None:
                raise ValueError("Failed deployment requires a failure code")
        elif self.failure_code is not None:
            raise ValueError("Only failed deployments may have a failure code")


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Explicit gate decision; production approval always requires a human reviewer."""

    decision_id: str
    tenant_id: str
    artifact_id: str
    from_stage: ModelLifecycleStage
    to_stage: ModelLifecycleStage
    decision: PromotionDecisionType
    evaluation_ids: tuple[str, ...]
    reason: str
    decided_at: datetime
    reviewer_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("Promotion decision identifier", self.decision_id),
            ("Promotion tenant identifier", self.tenant_id),
            ("Promotion artifact identifier", self.artifact_id),
            ("Promotion decision reason", self.reason),
        ):
            _require_text(name, value)
        if any(not item.strip() for item in self.evaluation_ids):
            raise ValueError("Promotion evaluation references must not be blank")
        if len(self.evaluation_ids) != len(set(self.evaluation_ids)):
            raise ValueError("Promotion evaluation references must be unique")
        if (
            self.decision is not PromotionDecisionType.REJECTED
            and not self.evaluation_ids
        ):
            raise ValueError("Approved promotion or rollback requires evaluation references")
        if self.decided_at.tzinfo is None:
            raise ValueError("Promotion decision time must be timezone-aware")
        if self.reviewer_id is not None:
            _require_text("Promotion reviewer identifier", self.reviewer_id)
        if (
            self.decision is PromotionDecisionType.APPROVED
            and self.to_stage is ModelLifecycleStage.PRODUCTION
            and self.reviewer_id is None
        ):
            raise ValueError("Production promotion requires explicit human approval")
        if self.decision is PromotionDecisionType.ROLLBACK_REQUIRED:
            if self.to_stage is not ModelLifecycleStage.ROLLED_BACK:
                raise ValueError("Rollback decisions must target rolled_back")
            if self.reviewer_id is None:
                raise ValueError("Rollback decisions require an accountable operator")


def retrieval_score_at_least(score: float | None, threshold: float) -> bool:
    """Compare ranking signals without treating them as probabilities."""

    if not math.isfinite(threshold):
        raise ValueError("Mining threshold must be finite")
    return score is not None and score >= threshold


def _require_text(name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
