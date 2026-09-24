"""Deterministic admission orchestration for explicitly reviewed invoice memories."""

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from typing import Callable, Generic, TypeVar

from invoice_intelligence.application.errors import (
    RemoteInferenceError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.admission import (
    MemoryAdmissionPolicy,
    MemoryAdmissionPolicyContext,
    MemoryAdmissionPolicyOutcome,
    MemoryAdmissionRepository,
    MemoryConflictRepository,
    MemoryFieldSchemaInspector,
    MemoryQualityAssessmentImage,
    MemoryQualityAssessmentProvider,
    MemoryQualityAssessmentRequest,
    ReviewerReliabilityRepository,
)
from invoice_intelligence.application.ports.business_persistence import (
    BusinessQueryRepository,
)
from invoice_intelligence.application.ports.document_processor import DocumentProcessor
from invoice_intelligence.application.ports.document_repository import (
    DocumentReferenceRepository,
)
from invoice_intelligence.application.ports.examples import ReviewedExampleRepository
from invoice_intelligence.application.ports.file_storage import FileStorage
from invoice_intelligence.application.ports.memory import (
    MemoryRecoveryRecord,
    MemoryRecoveryRepository,
    MemoryRecoveryStatus,
    MemoryRecoveryWorkLease,
    StoredCorrectionEvent,
)
from invoice_intelligence.application.ports.workflow import (
    ExtractionStateCodec,
    ExtractionValidator,
)
from invoice_intelligence.application.services.example_index_projection import (
    ExampleIndexProjectionService,
)
from invoice_intelligence.application.services.memory_quality_validation import (
    DeterministicMemoryQualityValidator,
    FieldRiskLevel,
    MemoryQualityValidationContext,
    OwnershipValidationFacts,
)
from invoice_intelligence.application.services.reviewed_example_generation import (
    ReviewedExampleGenerationService,
)
from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecision,
    MemoryAdmissionDecisionAuthority,
    MemoryAdmissionRecommendation,
    MemoryAdmissionRecord,
    MemoryAdmissionStatus,
    MemoryAssessmentSource,
    MemoryConflictStatus,
    MemoryQualityAssessment,
)
from invoice_intelligence.domain.document import (
    DocumentProcessingLimits,
    DocumentReference,
    UploadDocument,
    VisionImage,
)
from invoice_intelligence.domain.examples import ReviewedExample
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    ExtractionResult,
    FieldEvidence,
)
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    HumanCorrection,
    SignalVerdict,
    WorkflowIdentity,
)

InvoiceT = TypeVar("InvoiceT")
_LOGGER = logging.getLogger(__name__)
_AUTOMATION_ACTOR = "service:memory-admission-policy"
_INELIGIBLE_INDEX_STATUSES = frozenset(
    {
        MemoryAdmissionStatus.QUARANTINED,
        MemoryAdmissionStatus.REJECTED,
        MemoryAdmissionStatus.SUSPENDED,
        MemoryAdmissionStatus.INVALIDATED,
    }
)
_TERMINAL_AUTOMATION_STATUSES = frozenset(
    {
        MemoryAdmissionStatus.QUARANTINED,
        MemoryAdmissionStatus.REJECTED,
        MemoryAdmissionStatus.SUSPENDED,
        MemoryAdmissionStatus.INVALIDATED,
    }
)


@dataclass(frozen=True, slots=True)
class MemoryAdmissionPolicyConfig:
    """Versioned automatic-admission thresholds, keyed by configured field risk."""

    version: str
    field_risk_levels: Mapping[str, FieldRiskLevel]
    auto_approval_risk_levels: frozenset[FieldRiskLevel]
    minimum_quality_scores: Mapping[FieldRiskLevel, float]
    minimum_reviewer_reliability_score: float

    def __post_init__(self) -> None:
        if not self.version.strip() or self.version != self.version.strip():
            raise ValueError("Admission policy version must be non-empty and normalized")
        if not self.auto_approval_risk_levels:
            raise ValueError("At least one field risk level must permit automatic approval")
        missing = set(FieldRiskLevel).difference(self.minimum_quality_scores)
        if missing:
            raise ValueError("Every field risk level requires a quality threshold")
        if any(
            not 0.0 <= score <= 1.0
            for score in self.minimum_quality_scores.values()
        ):
            raise ValueError("Admission quality thresholds must be between zero and one")
        if not 0.0 <= self.minimum_reviewer_reliability_score <= 1.0:
            raise ValueError("Reviewer reliability threshold must be between zero and one")
        for field_path in self.field_risk_levels:
            if not field_path.strip() or field_path != field_path.strip():
                raise ValueError("Admission field risk paths must be normalized")

    def risk_for(self, field_path: str) -> FieldRiskLevel:
        return self.field_risk_levels.get(field_path, FieldRiskLevel.STANDARD)


class DeterministicMemoryAdmissionPolicy:
    """Merge deterministic facts and model advice without delegating authority."""

    def __init__(self, config: MemoryAdmissionPolicyConfig) -> None:
        self._config = config

    def evaluate(
        self,
        context: MemoryAdmissionPolicyContext,
    ) -> MemoryAdmissionPolicyOutcome:
        assessments = tuple(
            item
            for item in context.assessments
            if item.policy_version == self._config.version
        )
        deterministic = self._latest(assessments, MemoryAssessmentSource.DETERMINISTIC)
        advisory = self._latest(assessments, MemoryAssessmentSource.MODEL_ADVISORY)
        assessment_ids = tuple(item.assessment_id for item in assessments)
        open_conflict_ids = tuple(
            item.conflict_id
            for item in context.conflicts
            if item.status is MemoryConflictStatus.OPEN
        )

        if deterministic is None:
            return self._stay(context, "deterministic_assessment_missing")

        deterministic_failures = tuple(
            signal.code
            for signal in deterministic.signals
            if signal.verdict is SignalVerdict.FAILED
        )
        if deterministic_failures:
            status = (
                MemoryAdmissionStatus.REJECTED
                if deterministic.recommendation
                is MemoryAdmissionRecommendation.RECOMMEND_REJECTION
                else MemoryAdmissionStatus.QUARANTINED
            )
            return MemoryAdmissionPolicyOutcome(
                target_status=status,
                reason_codes=tuple(
                    dict.fromkeys(("deterministic_hard_failure", *deterministic_failures))
                ),
                supporting_assessment_ids=(deterministic.assessment_id,),
                supporting_conflict_ids=open_conflict_ids,
                requires_human_decision=status is MemoryAdmissionStatus.QUARANTINED,
            )

        if open_conflict_ids:
            return MemoryAdmissionPolicyOutcome(
                target_status=MemoryAdmissionStatus.QUARANTINED,
                reason_codes=("open_review_conflict",),
                supporting_assessment_ids=assessment_ids,
                supporting_conflict_ids=open_conflict_ids,
                requires_human_decision=True,
            )

        if deterministic.recommendation is not MemoryAdmissionRecommendation.RECOMMEND_APPROVAL:
            return MemoryAdmissionPolicyOutcome(
                target_status=MemoryAdmissionStatus.QUARANTINED,
                reason_codes=("deterministic_quality_not_approved",),
                supporting_assessment_ids=(deterministic.assessment_id,),
                supporting_conflict_ids=(),
                requires_human_decision=True,
            )

        if advisory is None:
            return self._stay(context, "model_advisory_missing")
        if advisory.recommendation is not MemoryAdmissionRecommendation.RECOMMEND_APPROVAL:
            return MemoryAdmissionPolicyOutcome(
                target_status=MemoryAdmissionStatus.QUARANTINED,
                reason_codes=tuple(
                    dict.fromkeys(
                        ("model_advisory_requires_review", *advisory.reason_codes)
                    )
                ),
                supporting_assessment_ids=(
                    deterministic.assessment_id,
                    advisory.assessment_id,
                ),
                supporting_conflict_ids=(),
                requires_human_decision=True,
            )

        risk = self._config.risk_for(context.reviewed_example.field_path)
        if risk not in self._config.auto_approval_risk_levels:
            return MemoryAdmissionPolicyOutcome(
                target_status=MemoryAdmissionStatus.QUARANTINED,
                reason_codes=(f"field_risk_requires_review.{risk.value}",),
                supporting_assessment_ids=(
                    deterministic.assessment_id,
                    advisory.assessment_id,
                ),
                supporting_conflict_ids=(),
                requires_human_decision=True,
            )

        minimum_score = self._config.minimum_quality_scores[risk]
        if (
            deterministic.quality_score < minimum_score
            or advisory.quality_score < minimum_score
        ):
            return MemoryAdmissionPolicyOutcome(
                target_status=MemoryAdmissionStatus.QUARANTINED,
                reason_codes=("quality_score_below_configured_threshold",),
                supporting_assessment_ids=(
                    deterministic.assessment_id,
                    advisory.assessment_id,
                ),
                supporting_conflict_ids=(),
                requires_human_decision=True,
            )

        reviewer = context.reviewer_profile
        if (
            reviewer is not None
            and reviewer.minimum_sample_met
            and reviewer.reliability_score
            < self._config.minimum_reviewer_reliability_score
        ):
            return MemoryAdmissionPolicyOutcome(
                target_status=MemoryAdmissionStatus.QUARANTINED,
                reason_codes=("reviewer_reliability_below_configured_threshold",),
                supporting_assessment_ids=(
                    deterministic.assessment_id,
                    advisory.assessment_id,
                ),
                supporting_conflict_ids=(),
                requires_human_decision=True,
            )

        return MemoryAdmissionPolicyOutcome(
            target_status=MemoryAdmissionStatus.APPROVED,
            reason_codes=("deterministic_policy_requirements_satisfied",),
            supporting_assessment_ids=(
                deterministic.assessment_id,
                advisory.assessment_id,
            ),
            supporting_conflict_ids=(),
            requires_human_decision=False,
        )

    @staticmethod
    def _latest(
        assessments: tuple[MemoryQualityAssessment, ...],
        source: MemoryAssessmentSource,
    ) -> MemoryQualityAssessment | None:
        matching = tuple(item for item in assessments if item.source is source)
        return max(matching, key=lambda item: (item.created_at, item.assessment_id), default=None)

    @staticmethod
    def _stay(
        context: MemoryAdmissionPolicyContext,
        reason_code: str,
    ) -> MemoryAdmissionPolicyOutcome:
        return MemoryAdmissionPolicyOutcome(
            target_status=context.admission.status,
            reason_codes=(reason_code,),
            supporting_assessment_ids=tuple(
                item.assessment_id for item in context.assessments
            ),
            supporting_conflict_ids=tuple(
                item.conflict_id
                for item in context.conflicts
                if item.status is MemoryConflictStatus.OPEN
            ),
            requires_human_decision=(
                context.admission.status is MemoryAdmissionStatus.QUARANTINED
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryAdmissionCaseResult:
    example: ReviewedExample
    admission: MemoryAdmissionRecord
    deterministic_assessment_id: str | None
    advisory_assessment_id: str | None
    projection_reconciled: bool
    advisory_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryReviewFactResult:
    """Durable review facts plus their independently recoverable outbox identity."""

    correction_events: tuple[StoredCorrectionEvent, ...]
    recovery_id: str
    trace_id: str
    status: MemoryRecoveryStatus


@dataclass(frozen=True, slots=True)
class MemoryCandidateRecoveryResult:
    recovery_id: str
    trace_id: str
    status: MemoryRecoveryStatus
    example_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MemoryAdmissionResult:
    """Outcome of an explicit admission-processing attempt."""

    cases: tuple[MemoryAdmissionCaseResult, ...]


class MemoryAdmissionService(Generic[InvoiceT]):
    """Separate durable review recording from retryable memory admission processing."""

    def __init__(
        self,
        *,
        recovery_repository: MemoryRecoveryRepository[InvoiceT],
        extraction_codec: ExtractionStateCodec,
        generation_service: ReviewedExampleGenerationService[InvoiceT],
        example_repository: ReviewedExampleRepository,
        admission_repository: MemoryAdmissionRepository,
        conflict_repository: MemoryConflictRepository,
        reviewer_reliability_repository: ReviewerReliabilityRepository,
        quality_validator: DeterministicMemoryQualityValidator[InvoiceT],
        quality_assessment_provider: MemoryQualityAssessmentProvider | None,
        admission_policy: MemoryAdmissionPolicy,
        schema_inspector: MemoryFieldSchemaInspector,
        extraction_validator: ExtractionValidator,
        document_repository: DocumentReferenceRepository,
        business_query_repository: BusinessQueryRepository,
        file_storage: FileStorage,
        document_processor: DocumentProcessor,
        document_limits: DocumentProcessingLimits,
        output_schema: type[InvoiceT],
        policy_version: str,
        reviewer_profile_version: str,
        mandatory_business_rules: tuple[str, ...],
        field_business_rules: Mapping[str, tuple[str, ...]],
        projection_service: ExampleIndexProjectionService | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name, value in (
            ("policy_version", policy_version),
            ("reviewer_profile_version", reviewer_profile_version),
        ):
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be non-empty and normalized")
        if not mandatory_business_rules:
            raise ValueError("Memory admission requires mandatory business rules")
        self._recoveries = recovery_repository
        self._extraction_codec = extraction_codec
        self._generation = generation_service
        self._examples = example_repository
        self._admissions = admission_repository
        self._conflicts = conflict_repository
        self._reviewers = reviewer_reliability_repository
        self._quality_validator = quality_validator
        self._assessment_provider = quality_assessment_provider
        self._admission_policy = admission_policy
        self._schema_inspector = schema_inspector
        self._extraction_validator = extraction_validator
        self._documents = document_repository
        self._business_queries = business_query_repository
        self._file_storage = file_storage
        self._document_processor = document_processor
        self._document_limits = document_limits
        self._output_schema = output_schema
        self._policy_version = policy_version
        self._reviewer_profile_version = reviewer_profile_version
        self._mandatory_business_rules = self._normalize_rules(mandatory_business_rules)
        self._field_business_rules: dict[str, tuple[str, ...]] = {}
        for field_path, rules in field_business_rules.items():
            if not field_path.strip() or field_path != field_path.strip():
                raise ValueError("Memory admission field business rule paths must be normalized")
            self._field_business_rules[field_path] = self._normalize_rules(rules)
        self._projection_service = projection_service
        self._clock = clock or (lambda: datetime.now(UTC))

    async def record_review_facts(
        self,
        *,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
        original_result: ExtractionResult[InvoiceT],
        reviewed_result: ExtractionResult[InvoiceT],
        review: HumanCorrection,
        correction_events: tuple[CorrectionEvent, ...],
    ) -> MemoryReviewFactResult:
        """Persist explicit review facts and a replay-safe recovery row atomically."""

        recovery = await self._recoveries.save_review_facts(
            identity=identity,
            tenant_id=tenant_id,
            document=document,
            original_result=original_result,
            reviewed_result=reviewed_result,
            review=review,
            correction_events=correction_events,
        )
        return MemoryReviewFactResult(
            correction_events=recovery.correction_events,
            recovery_id=recovery.recovery_id,
            trace_id=recovery.trace_id,
            status=recovery.status,
        )

    async def materialize_review_candidates(
        self,
        *,
        tenant_id: str,
        recovery_id: str,
        worker_id: str,
        lease_seconds: float = 60.0,
    ) -> MemoryCandidateRecoveryResult:
        """Materialize pending examples under a PostgreSQL lease, never assess them."""

        now = self._clock()
        lease = await self._recoveries.claim_memory_recovery(
            tenant_id,
            recovery_id,
            worker_id,
            now=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        if lease is None:
            current = await self._recoveries.get_memory_recovery(
                tenant_id,
                recovery_id,
            )
            if current is None:
                raise WorkflowPersistenceError("Memory recovery record is missing")
            return self._recovery_result(current)

        try:
            return await self.materialize_claimed_review_candidates(lease)
        except Exception as exc:
            error_at = self._clock()
            await self._recoveries.retry_memory_recovery(
                lease,
                next_attempt_at=error_at,
                error_code=self._recovery_error_code(exc),
                error_at=error_at,
            )
            raise

    async def get_review_recovery(
        self,
        tenant_id: str,
        recovery_id: str,
    ) -> MemoryRecoveryRecord | None:
        """Expose bounded recovery metadata to Workflow/API application callers."""

        return await self._recoveries.get_memory_recovery(tenant_id, recovery_id)

    async def materialize_claimed_review_candidates(
        self,
        lease: MemoryRecoveryWorkLease,
    ) -> MemoryCandidateRecoveryResult:
        """Materialize one already-claimed recovery for the standalone Worker."""

        record = lease.record
        original_result = self._extraction_codec.load(
            record.original_result_payload,
            self._output_schema,
        )
        reviewed_result = self._extraction_codec.load(
            record.reviewed_result_payload,
            self._output_schema,
        )
        generated = await self._generation.generate(
            identity=record.identity,
            tenant_id=record.tenant_id,
            document=record.document,
            original_result=original_result,
            reviewed_result=reviewed_result,
            review=record.review,
            correction_events=tuple(item.event for item in record.correction_events),
        )
        example_ids = tuple(item.example_id for item in generated.examples)
        await self.ensure_review_candidates(record.tenant_id, example_ids)
        completed = await self._recoveries.complete_memory_recovery(
            lease,
            example_ids=example_ids,
            completed_at=self._clock(),
        )
        if not completed:
            raise WorkflowPersistenceError("Memory recovery lease was lost")
        return MemoryCandidateRecoveryResult(
            recovery_id=record.recovery_id,
            trace_id=record.trace_id,
            status=MemoryRecoveryStatus.COMPLETED,
            example_ids=example_ids,
        )

    @staticmethod
    def _recovery_result(
        record: MemoryRecoveryRecord,
    ) -> MemoryCandidateRecoveryResult:
        return MemoryCandidateRecoveryResult(
            recovery_id=record.recovery_id,
            trace_id=record.trace_id,
            status=record.status,
            example_ids=record.example_ids,
        )

    @staticmethod
    def _recovery_error_code(exc: Exception) -> str:
        value = type(exc).__name__.strip().lower()
        normalized = "".join(
            character if character.isalnum() or character in {"_", ".", "-"} else "_"
            for character in value
        )
        return (normalized or "memory_recovery_error")[:128]

    async def ensure_review_candidates(
        self,
        tenant_id: str,
        example_ids: Sequence[str],
    ) -> tuple[MemoryAdmissionRecord, ...]:
        """Confirm candidate and admission-task durability without assessing them."""

        normalized_tenant = tenant_id.strip()
        if not normalized_tenant or normalized_tenant != tenant_id:
            raise ValueError("tenant_id must be non-empty and normalized")
        normalized_ids = tuple(example_ids)
        if any(not item.strip() or item != item.strip() for item in normalized_ids):
            raise ValueError("example_ids must be non-empty and normalized")
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("example_ids must be unique")
        records: list[MemoryAdmissionRecord] = []
        for example_id in normalized_ids:
            admission = await self._admissions.get(normalized_tenant, example_id)
            if admission is None:
                raise WorkflowPersistenceError(
                    "Reviewed example candidate has no admission task"
                )
            records.append(admission)
        return tuple(records)

    async def process_admission(
        self,
        *,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
        original_result: ExtractionResult[InvoiceT],
        reviewed_result: ExtractionResult[InvoiceT],
        examples: Sequence[ReviewedExample],
    ) -> MemoryAdmissionResult:
        """Assess existing candidates outside the invoice business Workflow."""

        candidates = tuple(examples)
        candidate_ids = tuple(example.example_id for example in candidates)
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("Admission candidates must be unique")
        if any(
            example.tenant_id != tenant_id
            or example.document_id != identity.document_id
            or example.run_id != identity.run_id
            for example in candidates
        ):
            raise ValueError("Admission candidate is outside the requested Workflow scope")

        ownership = await self._ownership(identity, tenant_id, document)
        validation = self._extraction_validator.validate(reviewed_result)
        field_validation_signals = tuple(
            signal
            for decision in validation.field_decisions
            for signal in decision.signals
        )
        evidence_by_path = {
            item.field_path: item for item in original_result.field_evidence
        }
        prepared_images: tuple[VisionImage, ...] | None = None
        image_error_code: str | None = None
        case_results: list[MemoryAdmissionCaseResult] = []

        for example in candidates:
            current = await self._require_admission(example)
            if current.status in _TERMINAL_AUTOMATION_STATUSES:
                reconciled = await self._reconcile_projection(example, current)
                case_results.append(
                    self._case_result(example, current, (), reconciled, None)
                )
                continue
            if current.status is MemoryAdmissionStatus.APPROVED:
                reconciled = await self._reconcile_projection(example, current)
                assessments = await self._admissions.list_assessments(
                    example.tenant_id,
                    example.example_id,
                )
                case_results.append(
                    self._case_result(example, current, assessments, reconciled, None)
                )
                continue

            conflicts = await self._conflicts.list_open_for_example(
                example.tenant_id,
                example.example_id,
            )
            support = await self._examples.get_support_diversity(
                example.tenant_id,
                example.fingerprint,
            )
            deterministic = self._quality_validator.validate(
                MemoryQualityValidationContext(
                    reviewed_example=example,
                    ownership=ownership,
                    page_quality=original_result.page_quality,
                    ocr_observations=original_result.ocr_observations,
                    field_validation_signals=field_validation_signals,
                    conflicts=conflicts,
                    support_diversity=support,
                    historical_distribution=None,
                )
            )
            deterministic_assessment = await self._admissions.save_assessment(
                deterministic.assessment
            )

            existing_assessments = await self._admissions.list_assessments(
                example.tenant_id,
                example.example_id,
            )
            advisory_assessment = self._existing_advisory(existing_assessments)
            advisory_error_code: str | None = None
            if (
                not deterministic.blocking_failure_codes
                and self._assessment_provider is not None
                and advisory_assessment is None
            ):
                field_evidence = evidence_by_path.get(example.field_path)
                if field_evidence is not None and field_evidence.source is EvidenceSource.VISUAL:
                    if prepared_images is None and image_error_code is None:
                        try:
                            prepared_images = await self._load_images(document)
                        except Exception as exc:
                            image_error_code = type(exc).__name__
                            _LOGGER.warning(
                                "memory_admission_image_unavailable",
                                extra={
                                    "tenant_id": example.tenant_id,
                                    "example_id": example.example_id,
                                    "error_type": image_error_code,
                                },
                            )
                    if prepared_images is not None:
                        try:
                            advisory = await self._assessment_provider.assess(
                                self._assessment_request(
                                    example,
                                    field_evidence,
                                    prepared_images,
                                    deterministic_assessment,
                                )
                            )
                            self._validate_advisory(example, advisory)
                            advisory_assessment = await self._admissions.save_assessment(
                                advisory
                            )
                        except RemoteInferenceError as exc:
                            advisory_error_code = type(exc).__name__
                            _LOGGER.warning(
                                "memory_admission_model_unavailable",
                                extra={
                                    "tenant_id": example.tenant_id,
                                    "example_id": example.example_id,
                                    "error_type": advisory_error_code,
                                },
                            )
                    else:
                        advisory_error_code = image_error_code

            assessments = tuple(
                {
                    item.assessment_id: item
                    for item in (
                        deterministic_assessment,
                        advisory_assessment,
                    )
                    if item is not None
                }.values()
            )
            reviewer = await self._reviewers.get(
                example.tenant_id,
                example.reviewer_id,
                self._reviewer_profile_version,
            )
            outcome = self._admission_policy.evaluate(
                MemoryAdmissionPolicyContext(
                    admission=current,
                    reviewed_example=example,
                    assessments=assessments,
                    conflicts=conflicts,
                    reviewer_profile=reviewer,
                )
            )
            if outcome.target_status is not current.status:
                current = await self._append_policy_decision(current, outcome)
            reconciled = await self._reconcile_projection(example, current)
            case_results.append(
                MemoryAdmissionCaseResult(
                    example=example,
                    admission=current,
                    deterministic_assessment_id=deterministic_assessment.assessment_id,
                    advisory_assessment_id=(
                        advisory_assessment.assessment_id
                        if advisory_assessment is not None
                        else None
                    ),
                    projection_reconciled=reconciled,
                    advisory_error_code=advisory_error_code,
                )
            )

        return MemoryAdmissionResult(
            cases=tuple(case_results),
        )

    async def quarantine_processing_failure(
        self,
        *,
        example: ReviewedExample,
        reason_code: str,
    ) -> MemoryAdmissionCaseResult:
        """Quarantine exhausted or permanent Worker failures without deleting facts."""

        normalized_reason = reason_code.strip()
        if (
            not normalized_reason
            or normalized_reason != reason_code
            or len(normalized_reason) > 128
        ):
            raise ValueError("Worker failure reason_code must be normalized and bounded")
        current = await self._require_admission(example)
        if current.status not in {
            MemoryAdmissionStatus.PENDING,
            MemoryAdmissionStatus.APPROVED,
        }:
            assessments = await self._admissions.list_assessments(
                example.tenant_id,
                example.example_id,
            )
            reconciled = await self._reconcile_projection(example, current)
            return self._case_result(example, current, assessments, reconciled, None)

        assessments = await self._admissions.list_assessments(
            example.tenant_id,
            example.example_id,
        )
        conflicts = await self._conflicts.list_open_for_example(
            example.tenant_id,
            example.example_id,
        )
        outcome = MemoryAdmissionPolicyOutcome(
            target_status=MemoryAdmissionStatus.QUARANTINED,
            reason_codes=("worker_processing_quarantined", normalized_reason),
            supporting_assessment_ids=tuple(
                item.assessment_id for item in assessments
            ),
            supporting_conflict_ids=tuple(
                item.conflict_id
                for item in conflicts
                if item.status is MemoryConflictStatus.OPEN
            ),
            requires_human_decision=True,
        )
        updated = await self._append_policy_decision(current, outcome)
        reconciled = await self._reconcile_projection(example, updated)
        return self._case_result(example, updated, assessments, reconciled, None)

    async def _ownership(
        self,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
    ) -> OwnershipValidationFacts:
        persisted_document = await self._documents.get_document(
            document.document_id,
            tenant_id,
        )
        run = await self._business_queries.get_run(identity.run_id, tenant_id)
        return OwnershipValidationFacts(
            trusted_tenant_id=tenant_id,
            document_id=document.document_id,
            document_tenant_id=tenant_id if persisted_document is not None else None,
            run_id=identity.run_id,
            run_tenant_id=tenant_id if run is not None else None,
            run_document_id=(
                run.identity.document_id if run is not None else None
            ),
        )

    async def _require_admission(
        self,
        example: ReviewedExample,
    ) -> MemoryAdmissionRecord:
        admission = await self._admissions.get(example.tenant_id, example.example_id)
        if admission is None:
            raise WorkflowPersistenceError("Reviewed example has no admission record")
        return admission

    def _assessment_request(
        self,
        example: ReviewedExample,
        field_evidence: FieldEvidence,
        images: tuple[VisionImage, ...],
        deterministic: MemoryQualityAssessment,
    ) -> MemoryQualityAssessmentRequest:
        page_number = example.evidence_reference.page_number
        selected = tuple(
            image for image in images if image.page_number == page_number
        )
        reference = example.evidence_reference.image_reference
        temporary_images = tuple(
            MemoryQualityAssessmentImage(
                evidence_reference=(
                    reference
                    or f"document:{example.document_id}#page={image.page_number}"
                ),
                image=image,
            )
            for image in selected
        )
        rules = tuple(
            dict.fromkeys(
                (
                    *self._mandatory_business_rules,
                    *self._field_business_rules.get(example.field_path, ()),
                )
            )
        )
        requested_at = self._now()
        request_material = (
            f"{example.tenant_id}\0{example.example_id}\0"
            f"{deterministic.input_fingerprint}\0{self._policy_version}"
        )
        return MemoryQualityAssessmentRequest(
            request_id=sha256(request_material.encode("utf-8")).hexdigest(),
            tenant_id=example.tenant_id,
            example_id=example.example_id,
            document_type=example.document_type,
            field_path=example.field_path,
            schema_version=example.schema_version,
            current_field_evidence=field_evidence,
            temporary_images=temporary_images,
            model_value=example.model_value,
            reviewed_value=example.reviewed_value,
            field_semantic_definition=self._schema_inspector.describe_field(
                self._output_schema,
                example.document_type,
                example.field_path,
            ),
            deterministic_signals=deterministic.signals,
            mandatory_business_rules=rules,
            policy_version=self._policy_version,
            requested_at=requested_at,
        )

    async def _load_images(
        self,
        document: DocumentReference,
    ) -> tuple[VisionImage, ...]:
        content = await self._file_storage.read(document.storage_uri)
        if not compare_digest(sha256(content).hexdigest(), document.checksum):
            raise ValueError("Stored document checksum does not match admission evidence")
        inspected = await self._document_processor.inspect(
            UploadDocument(
                filename=None,
                declared_mime_type=document.mime_type,
                content=content,
            ),
            self._document_limits,
        )
        return await self._document_processor.to_vision_images(
            content,
            inspected,
            self._document_limits,
        )

    async def _append_policy_decision(
        self,
        current: MemoryAdmissionRecord,
        outcome: MemoryAdmissionPolicyOutcome,
    ) -> MemoryAdmissionRecord:
        material = self._canonical(
            {
                "tenant_id": current.tenant_id,
                "example_id": current.example_id,
                "previous_status": current.status.value,
                "target_status": outcome.target_status.value,
                "revision": current.revision + 1,
                "reason_codes": list(outcome.reason_codes),
                "assessment_ids": list(outcome.supporting_assessment_ids),
                "conflict_ids": list(outcome.supporting_conflict_ids),
                "policy_version": self._policy_version,
            }
        )
        stable_hash = sha256(material.encode("utf-8")).hexdigest()
        assessments = await self._admissions.list_assessments(
            current.tenant_id,
            current.example_id,
        )
        decided_at = max(
            (item.created_at for item in assessments),
            default=current.updated_at,
        )
        decision = MemoryAdmissionDecision(
            decision_id=sha256(
                f"memory-admission-decision\0{stable_hash}".encode("utf-8")
            ).hexdigest(),
            tenant_id=current.tenant_id,
            example_id=current.example_id,
            previous_status=current.status,
            status=outcome.target_status,
            authority=MemoryAdmissionDecisionAuthority.DETERMINISTIC_POLICY,
            decided_by=_AUTOMATION_ACTOR,
            reason=self._decision_reason(outcome.target_status),
            reason_codes=outcome.reason_codes,
            assessment_ids=outcome.supporting_assessment_ids,
            conflict_ids=outcome.supporting_conflict_ids,
            policy_version=self._policy_version,
            idempotency_key_hash=sha256(
                f"memory-admission-transition\0{stable_hash}".encode("utf-8")
            ).hexdigest(),
            revision=current.revision + 1,
            decided_at=decided_at,
        )
        try:
            return await self._admissions.append_decision(
                decision,
                expected_revision=current.revision,
            )
        except WorkflowPersistenceError:
            latest = await self._admissions.get(current.tenant_id, current.example_id)
            if latest is not None and latest.status is outcome.target_status:
                return latest
            raise

    def _validate_advisory(
        self,
        example: ReviewedExample,
        assessment: MemoryQualityAssessment,
    ) -> None:
        if (
            assessment.source is not MemoryAssessmentSource.MODEL_ADVISORY
            or assessment.tenant_id != example.tenant_id
            or assessment.example_id != example.example_id
            or assessment.policy_version != self._policy_version
        ):
            raise RemoteInferenceError(
                "Memory quality Provider returned an out-of-scope assessment"
            )

    def _existing_advisory(
        self,
        assessments: tuple[MemoryQualityAssessment, ...],
    ) -> MemoryQualityAssessment | None:
        provider = self._assessment_provider
        if provider is None:
            return None
        matching = tuple(
            item
            for item in assessments
            if item.source is MemoryAssessmentSource.MODEL_ADVISORY
            and item.policy_version == self._policy_version
            and item.model_version == provider.model_version
            and item.prompt_version == provider.prompt_version
        )
        return max(
            matching,
            key=lambda item: (item.created_at, item.assessment_id),
            default=None,
        )

    async def _reconcile_projection(
        self,
        example: ReviewedExample,
        admission: MemoryAdmissionRecord,
    ) -> bool:
        if self._projection_service is None:
            return True
        try:
            if admission.status is MemoryAdmissionStatus.APPROVED:
                await self._projection_service.schedule_approved_example(
                    example.tenant_id,
                    example.example_id,
                    example.schema_version,
                )
            elif admission.status in _INELIGIBLE_INDEX_STATUSES:
                await self._projection_service.remove_derived_example(
                    example.tenant_id,
                    example.example_id,
                    example.schema_version,
                )
            return True
        except Exception as exc:
            _LOGGER.error(
                "memory_admission_projection_reconciliation_failed",
                extra={
                    "tenant_id": example.tenant_id,
                    "example_id": example.example_id,
                    "admission_status": admission.status.value,
                    "error_type": type(exc).__name__,
                },
            )
            return False

    @staticmethod
    def _case_result(
        example: ReviewedExample,
        admission: MemoryAdmissionRecord,
        assessments: tuple[MemoryQualityAssessment, ...],
        projection_reconciled: bool,
        advisory_error_code: str | None,
    ) -> MemoryAdmissionCaseResult:
        deterministic = DeterministicMemoryAdmissionPolicy._latest(
            assessments,
            MemoryAssessmentSource.DETERMINISTIC,
        )
        advisory = DeterministicMemoryAdmissionPolicy._latest(
            assessments,
            MemoryAssessmentSource.MODEL_ADVISORY,
        )
        return MemoryAdmissionCaseResult(
            example=example,
            admission=admission,
            deterministic_assessment_id=(
                deterministic.assessment_id if deterministic is not None else None
            ),
            advisory_assessment_id=(
                advisory.assessment_id if advisory is not None else None
            ),
            projection_reconciled=projection_reconciled,
            advisory_error_code=advisory_error_code,
        )

    @staticmethod
    def _decision_reason(status: MemoryAdmissionStatus) -> str:
        return {
            MemoryAdmissionStatus.APPROVED: (
                "Configured deterministic admission policy approved the reviewed case"
            ),
            MemoryAdmissionStatus.QUARANTINED: (
                "Reviewed case requires an attributable second review"
            ),
            MemoryAdmissionStatus.REJECTED: (
                "Reviewed case failed a deterministic admission requirement"
            ),
            MemoryAdmissionStatus.SUSPENDED: "Reviewed case admission was suspended",
            MemoryAdmissionStatus.INVALIDATED: "Reviewed case admission was invalidated",
            MemoryAdmissionStatus.PENDING: "Reviewed case remains pending admission",
        }[status]

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Memory admission clock must return a timezone-aware datetime")
        return value

    @staticmethod
    def _normalize_rules(rules: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(rule.strip() for rule in rules)
        if any(not rule for rule in normalized):
            raise ValueError("Memory admission business rules cannot be blank")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Memory admission business rules must be unique")
        return normalized

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
