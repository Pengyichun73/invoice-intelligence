"""Tenant-scoped governance use cases for reviewed-example memory."""

import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, TypeVar
from uuid import uuid4

from invoice_intelligence.application.errors import (
    ApplicationServiceError,
    BadRequestError,
    ForbiddenError,
    IdempotencyInProgressError,
    ResourceConflictError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UnprocessableEntityError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.admission import (
    MemoryAdmissionRepository,
    MemoryConflictRepository,
)
from invoice_intelligence.application.ports.business_persistence import (
    IdempotencyRepository,
    IdempotencyStatus,
)
from invoice_intelligence.application.ports.evaluation import EvaluationRunRepository
from invoice_intelligence.application.ports.examples import IndexProjectionRepository
from invoice_intelligence.application.ports.field_semantics import (
    FieldAliasCandidateRepository,
)
from invoice_intelligence.application.ports.governance import (
    MemoryGovernanceRepository,
    RetrievalTelemetryRepository,
)
from invoice_intelligence.application.ports.observability import (
    OCRMetricsRepository,
    OCRMetricsSummary,
)
from invoice_intelligence.application.services.example_index_projection import (
    ExampleIndexProjectionService,
    ProjectionBatchResult,
)
from invoice_intelligence.application.services.field_alias_learning import (
    FieldAliasLearningService,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.application.services.field_semantic_index_projection import (
    FieldSemanticIndexProjectionService,
    FieldSemanticProjectionBatchResult,
)
from invoice_intelligence.application.services.idempotency import normalize_idempotency_key
from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecision,
    MemoryAdmissionDecisionAuthority,
    MemoryAdmissionRecord,
    MemoryAdmissionStatus,
    MemoryAssessmentSource,
    MemoryConflictRecord,
    MemoryConflictReevaluationTarget,
    MemoryConflictReevaluationTargetType,
    MemoryConflictResolutionDecision,
    MemoryConflictStatus,
    MemoryQualityAssessment,
)
from invoice_intelligence.domain.evaluation import EvaluationRun
from invoice_intelligence.domain.examples import (
    ExampleIndexProjectionState,
    ExampleLabelType,
    IndexProjectionStatus,
    IndexVersion,
    ModelVersion,
    PromptVersion,
    ReviewedExample,
)
from invoice_intelligence.domain.field_semantics import (
    FieldAliasCandidate,
    FieldAliasStatus,
    FieldAliasSupportSummary,
    FieldSemanticCatalogVersion,
    FieldSemanticDefinition,
    FieldSemanticIndexVersionRecord,
)
from invoice_intelligence.domain.governance import (
    GovernanceAction,
    GovernanceAuditEvent,
    IndexGovernanceRecord,
    MemoryPermission,
    RetrievalFeedback,
    RetrievalFeedbackLabel,
    RetrievalMetricSummary,
    TrustedTenantContext,
)
from invoice_intelligence.domain.workflow import SignalVerdict

_LOGGER = logging.getLogger(__name__)
_EnumT = TypeVar("_EnumT")
_DATA_IMAGE_RE = re.compile(r"data\s*:\s*image\s*/", re.IGNORECASE)
_BASE64_BLOCK_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{256,}={0,2}")
_EMAIL_RE = re.compile(
    r"(?<![\w.])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"
)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\s()-]?){7,}\d(?!\w)")
_LONG_NUMBER_RE = re.compile(r"(?<!\d)\d{7,}(?!\d)")


@dataclass(frozen=True, slots=True)
class MemoryExamplePage:
    items: tuple[ReviewedExample, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MemoryExampleProjectionPage:
    example_id: str
    admission_status: MemoryAdmissionStatus
    eligible_for_long_term_retrieval: bool
    items: tuple[ExampleIndexProjectionState, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if any(item.example_id != self.example_id for item in self.items):
            raise ValueError("Projection page contains another example")


@dataclass(frozen=True, slots=True)
class GovernanceAuditPage:
    items: tuple[GovernanceAuditEvent, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class GovernanceOperationResult:
    action: GovernanceAction
    resource_id: str
    changed_count: int
    audit_id: str
    resource_version: str | None
    trace_id: str | None
    performed_at: datetime


@dataclass(frozen=True, slots=True)
class IndexRebuildSpec:
    index_version: IndexVersion
    schema_version: str
    dense_model_version: ModelVersion
    sparse_model_version: ModelVersion | None
    rerank_model_version: ModelVersion | None
    prompt_version: PromptVersion
    reason: str

    def __post_init__(self) -> None:
        if not self.schema_version.strip() or self.schema_version != self.schema_version.strip():
            raise ValueError("schema_version must be non-empty and normalized")
        if not self.reason.strip() or self.reason != self.reason.strip():
            raise ValueError("reason must be non-empty and normalized")


@dataclass(frozen=True, slots=True)
class MemoryIndexView:
    index: IndexGovernanceRecord
    metrics: RetrievalMetricSummary
    audit_event: GovernanceAuditEvent | None = None


@dataclass(frozen=True, slots=True)
class IndexProjectionExecution:
    batch: ProjectionBatchResult
    index: MemoryIndexView


@dataclass(frozen=True, slots=True)
class FieldSemanticProjectionExecution:
    batch: FieldSemanticProjectionBatchResult
    index: FieldSemanticIndexVersionRecord


@dataclass(frozen=True, slots=True)
class MemoryAdmissionGovernanceItem:
    admission: MemoryAdmissionRecord
    example: ReviewedExample

    def __post_init__(self) -> None:
        if (
            self.admission.tenant_id != self.example.tenant_id
            or self.admission.example_id != self.example.example_id
        ):
            raise ValueError("Admission governance item has inconsistent scope")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionPage:
    items: tuple[MemoryAdmissionGovernanceItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MemoryAdmissionDetail:
    item: MemoryAdmissionGovernanceItem
    assessments: tuple[MemoryQualityAssessment, ...]
    decisions: tuple[MemoryAdmissionDecision, ...]
    conflicts: tuple[MemoryConflictRecord, ...]


@dataclass(frozen=True, slots=True)
class MemoryAdmissionDecisionResult:
    item: MemoryAdmissionGovernanceItem
    decision: MemoryAdmissionDecision
    projection_reconciled: bool
    audit_event: GovernanceAuditEvent | None = None


@dataclass(frozen=True, slots=True)
class MemoryAdmissionBatchDecisionItem:
    admission_id: str
    result: MemoryAdmissionDecisionResult | None = None
    error_type: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        succeeded = self.result is not None
        failed = self.error_type is not None and self.error_message is not None
        if succeeded == failed:
            raise ValueError("Batch decision item must contain one outcome")


@dataclass(frozen=True, slots=True)
class MemoryAdmissionBatchDecisionResult:
    target_status: MemoryAdmissionStatus
    items: tuple[MemoryAdmissionBatchDecisionItem, ...]

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("Batch decision result must not be empty")


@dataclass(frozen=True, slots=True)
class FieldAliasGovernanceItem:
    candidate: FieldAliasCandidate
    support: FieldAliasSupportSummary
    source_reviewer_count: int
    conflict_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FieldSemanticGovernancePage:
    definitions: tuple[FieldSemanticDefinition, ...]
    alias_candidates: tuple[FieldAliasGovernanceItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class FieldSemanticConflictPage:
    items: tuple[MemoryConflictRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MemoryConflictResolutionResult:
    conflict: MemoryConflictRecord
    decision: MemoryConflictResolutionDecision
    reevaluation_registered: bool
    audit_event: GovernanceAuditEvent | None = None


@dataclass(frozen=True, slots=True)
class FieldAliasDecisionResult:
    item: FieldAliasGovernanceItem
    promoted_catalog_version: FieldSemanticCatalogVersion | None
    audit_event: GovernanceAuditEvent | None = None


@dataclass(frozen=True, slots=True)
class MemoryFeedbackResult:
    feedback: RetrievalFeedback
    audit_event: GovernanceAuditEvent | None


class MemoryGovernanceService:
    """Apply governance policy while preserving PostgreSQL/Milvus boundaries."""

    def __init__(
        self,
        *,
        example_repository: IndexProjectionRepository,
        governance_repository: MemoryGovernanceRepository,
        telemetry_repository: RetrievalTelemetryRepository,
        evaluation_repository: EvaluationRunRepository,
        idempotency_repository: IdempotencyRepository,
        projection_service: ExampleIndexProjectionService | None,
        admission_repository: MemoryAdmissionRepository,
        conflict_repository: MemoryConflictRepository,
        field_alias_candidate_repository: FieldAliasCandidateRepository,
        field_alias_learning_service: FieldAliasLearningService[Any],
        field_semantic_catalog: FieldSemanticCatalog[Any],
        admission_policy_version: str,
        conflict_resolution_policy_version: str,
        schema_version: str,
        require_distinct_second_reviewer: bool,
        ocr_metrics_repository: OCRMetricsRepository | None = None,
        field_semantic_projection_service: (
            FieldSemanticIndexProjectionService[Any] | None
        ) = None,
    ) -> None:
        self._examples = example_repository
        self._governance = governance_repository
        self._telemetry = telemetry_repository
        self._evaluations = evaluation_repository
        self._idempotency = idempotency_repository
        self._projection_service = projection_service
        self._admissions = admission_repository
        self._conflicts = conflict_repository
        self._field_alias_candidates = field_alias_candidate_repository
        self._field_alias_learning = field_alias_learning_service
        self._field_semantics = field_semantic_catalog
        self._admission_policy_version = self._normalize_identifier(
            "admission_policy_version",
            admission_policy_version,
        )
        self._conflict_resolution_policy_version = self._normalize_identifier(
            "conflict_resolution_policy_version",
            conflict_resolution_policy_version,
        )
        self._schema_version = self._normalize_identifier(
            "schema_version",
            schema_version,
        )
        self._require_distinct_second_reviewer = require_distinct_second_reviewer
        self._ocr_metrics = ocr_metrics_repository
        self._field_semantic_projection = field_semantic_projection_service

    async def get_ocr_metrics(
        self,
        context: TrustedTenantContext,
    ) -> OCRMetricsSummary:
        self._require(context, MemoryPermission.READ)
        if self._ocr_metrics is None:
            raise ServiceUnavailableError("OCR metric persistence is not configured")
        return await self._ocr_metrics.summarize()

    async def project_field_semantic_index(
        self,
        context: TrustedTenantContext,
        index_version: str,
        *,
        limit: int | None = None,
    ) -> FieldSemanticProjectionExecution:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        service = self._field_semantic_projection
        if service is None:
            raise ServiceUnavailableError("Field semantic projection is not configured")
        if limit is not None and not 1 <= limit <= 500:
            raise BadRequestError("Projection limit must be between 1 and 500")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        if await service.get_index_version(context.tenant_id, version) is None:
            raise ResourceNotFoundError("Field semantic index version was not found")
        batch = await service.project_pending(
            context.tenant_id,
            version,
            limit=limit,
            trace_id=context.trace_id,
        )
        index = await service.get_index_version(context.tenant_id, version)
        if index is None:
            raise ResourceNotFoundError("Field semantic index version was not found")
        return FieldSemanticProjectionExecution(batch=batch, index=index)

    async def register_field_semantic_index(
        self,
        context: TrustedTenantContext,
        *,
        index_version: str,
        schema_version: str,
        catalog_version: str,
        dense_model_version: str,
        sparse_model_version: str | None,
    ) -> FieldSemanticIndexVersionRecord:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        service = self._field_semantic_projection
        if service is None:
            raise ServiceUnavailableError("Field semantic projection is not configured")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        await service.register_index_version(
            context.tenant_id,
            version,
            self._normalize_identifier("schema_version", schema_version),
            FieldSemanticCatalogVersion(
                self._normalize_identifier("catalog_version", catalog_version)
            ),
            ModelVersion(
                self._normalize_identifier("dense_model_version", dense_model_version)
            ),
            (
                ModelVersion(
                    self._normalize_identifier(
                        "sparse_model_version",
                        sparse_model_version,
                    )
                )
                if sparse_model_version is not None
                else None
            ),
        )
        index = await service.get_index_version(context.tenant_id, version)
        if index is None:
            raise ResourceNotFoundError("Field semantic index version was not found")
        return index

    async def get_field_semantic_index(
        self,
        context: TrustedTenantContext,
        index_version: str,
    ) -> FieldSemanticIndexVersionRecord:
        self._require(context, MemoryPermission.READ)
        service = self._field_semantic_projection
        if service is None:
            raise ServiceUnavailableError("Field semantic projection is not configured")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        index = await service.get_index_version(context.tenant_id, version)
        if index is None:
            raise ResourceNotFoundError("Field semantic index version was not found")
        return index

    async def activate_field_semantic_index(
        self,
        context: TrustedTenantContext,
        index_version: str,
    ) -> FieldSemanticIndexVersionRecord:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        service = self._field_semantic_projection
        if service is None:
            raise ServiceUnavailableError("Field semantic projection is not configured")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        try:
            await service.activate_index_version(context.tenant_id, version)
        except ValueError as exc:
            raise ResourceConflictError(str(exc)) from exc
        index = await service.get_index_version(context.tenant_id, version)
        if index is None:
            raise ResourceNotFoundError("Field semantic index version was not found")
        return index

    async def rollback_field_semantic_index(self, context: TrustedTenantContext, index_version: str) -> FieldSemanticIndexVersionRecord:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        service = self._field_semantic_projection
        if service is None:
            raise ServiceUnavailableError("Field semantic projection is not configured")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        target = await service.get_index_version(context.tenant_id, version)
        if target is None or not target.is_valid or not target.ready_for_activation:
            raise ResourceConflictError("Rollback target is not a fully verified valid index")
        await service.activate_index_version(context.tenant_id, version)
        return target

    async def list_examples(
        self,
        context: TrustedTenantContext,
        *,
        schema_version: str | None,
        field_path: str | None,
        label_type: ExampleLabelType | None,
        is_valid: bool | None,
        limit: int,
        cursor: str | None,
    ) -> MemoryExamplePage:
        self._require(context, MemoryPermission.READ)
        if not 1 <= limit <= 100:
            raise BadRequestError("limit must be between 1 and 100")
        schema_version = self._normalize_optional_identifier(
            "schema_version",
            schema_version,
        )
        field_path = self._normalize_optional_identifier("field_path", field_path)
        cursor = self._normalize_optional_identifier("cursor", cursor)
        items = await self._examples.list_for_governance(
            context.tenant_id,
            schema_version=schema_version,
            field_path=field_path,
            label_type=label_type,
            is_valid=is_valid,
            limit=limit + 1,
            after_example_id=cursor,
        )
        has_more = len(items) > limit
        page = items[:limit]
        return MemoryExamplePage(
            items=page,
            next_cursor=page[-1].example_id if has_more and page else None,
        )

    async def list_audits(
        self,
        context: TrustedTenantContext,
        *,
        action: GovernanceAction | None,
        resource_type: str | None,
        resource_id: str | None,
        trace_id: str | None,
        started_at: datetime | None,
        ended_at: datetime | None,
        limit: int,
        cursor: str | None,
    ) -> GovernanceAuditPage:
        self._require(context, MemoryPermission.READ)
        if not 1 <= limit <= 100:
            raise BadRequestError("limit must be between 1 and 100")
        resource_type = self._normalize_optional_identifier(
            "resource_type",
            resource_type,
        )
        resource_id = self._normalize_optional_identifier("resource_id", resource_id)
        trace_id = self._normalize_optional_identifier("trace_id", trace_id)
        for name, value in (("started_at", started_at), ("ended_at", ended_at)):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise BadRequestError(f"{name} must include a timezone")
        if started_at is not None and ended_at is not None and started_at > ended_at:
            raise BadRequestError("started_at must not be later than ended_at")
        cursor = self._normalize_optional_identifier("cursor", cursor)
        records = await self._governance.list_audits(
            context.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            trace_id=trace_id,
            started_at=started_at,
            ended_at=ended_at,
            limit=limit + 1,
            after_audit_id=cursor,
        )
        has_more = len(records) > limit
        page = tuple(records[:limit])
        return GovernanceAuditPage(
            items=page,
            next_cursor=page[-1].audit_id if has_more and page else None,
        )

    async def get_example(
        self,
        context: TrustedTenantContext,
        example_id: str,
    ) -> ReviewedExample:
        self._require(context, MemoryPermission.READ)
        return await self._get_example(
            context.tenant_id,
            self._normalize_identifier("example_id", example_id),
        )

    async def list_example_projections(
        self,
        context: TrustedTenantContext,
        example_id: str,
        *,
        index_version: str | None,
        status: IndexProjectionStatus | None,
        limit: int,
        cursor: str | None,
    ) -> MemoryExampleProjectionPage:
        """Read PostgreSQL projection facts without querying the derived index."""

        self._require(context, MemoryPermission.READ)
        if not 1 <= limit <= 100:
            raise BadRequestError("limit must be between 1 and 100")
        example_id = self._normalize_identifier("example_id", example_id)
        normalized_version = self._normalize_optional_identifier(
            "index_version",
            index_version,
        )
        cursor = self._normalize_optional_identifier("cursor", cursor)
        example = await self._get_example(context.tenant_id, example_id)
        admission = await self._admissions.get(context.tenant_id, example_id)
        if admission is None:
            raise ResourceConflictError("Reviewed example has no admission record")
        records = await self._examples.list_example_projections(
            context.tenant_id,
            example_id,
            index_version=(
                IndexVersion(normalized_version)
                if normalized_version is not None
                else None
            ),
            status=status,
            limit=limit + 1,
            after_projection_id=cursor,
        )
        has_more = len(records) > limit
        page = records[:limit]
        return MemoryExampleProjectionPage(
            example_id=example_id,
            admission_status=admission.status,
            eligible_for_long_term_retrieval=(
                admission.eligible_for_long_term_retrieval
                and example.is_reviewed
                and example.is_valid
            ),
            items=page,
            next_cursor=(
                page[-1].projection_id if has_more and page else None
            ),
        )

    async def _get_example(
        self,
        tenant_id: str,
        example_id: str,
    ) -> ReviewedExample:
        example = await self._examples.get_for_governance(
            tenant_id,
            example_id,
        )
        if example is None:
            raise ResourceNotFoundError("Reviewed example was not found")
        return example

    async def list_admissions(
        self,
        context: TrustedTenantContext,
        *,
        statuses: Sequence[MemoryAdmissionStatus],
        limit: int,
        cursor: str | None,
    ) -> MemoryAdmissionPage:
        self._require(context, MemoryPermission.GOVERN)
        if not 1 <= limit <= 100:
            raise BadRequestError("limit must be between 1 and 100")
        normalized_statuses = self._unique_values("admission status", statuses)
        cursor = self._normalize_optional_identifier("cursor", cursor)
        records = await self._admissions.list_by_status(
            context.tenant_id,
            normalized_statuses,
            limit=limit + 1,
            after_example_id=cursor,
        )
        has_more = len(records) > limit
        page = records[:limit]
        items = tuple(
            [await self._admission_item(context.tenant_id, record) for record in page]
        )
        return MemoryAdmissionPage(
            items=items,
            next_cursor=(
                items[-1].admission.example_id if has_more and items else None
            ),
        )

    async def get_admission(
        self,
        context: TrustedTenantContext,
        admission_id: str,
    ) -> MemoryAdmissionDetail:
        self._require(context, MemoryPermission.GOVERN)
        admission_id = self._normalize_identifier("admission_id", admission_id)
        admission = await self._require_admission(context.tenant_id, admission_id)
        item = await self._admission_item(context.tenant_id, admission)
        assessments = await self._admissions.list_assessments(
            context.tenant_id,
            admission.example_id,
        )
        decisions = await self._admissions.list_decisions(
            context.tenant_id,
            admission.example_id,
        )
        conflicts = await self._conflicts.list_open_for_example(
            context.tenant_id,
            admission.example_id,
        )
        return MemoryAdmissionDetail(
            item=item,
            assessments=assessments,
            decisions=decisions,
            conflicts=conflicts,
        )

    async def decide_admission(
        self,
        context: TrustedTenantContext,
        admission_id: str,
        target_status: MemoryAdmissionStatus,
        *,
        reason: str,
        expected_revision: int,
        idempotency_key: str | None,
    ) -> MemoryAdmissionDecisionResult:
        self._require(context, MemoryPermission.GOVERN)
        admission_id = self._normalize_identifier("admission_id", admission_id)
        reason = self._normalize_reason(reason)
        key = normalize_idempotency_key(idempotency_key)
        if expected_revision <= 0:
            raise BadRequestError("expected_revision must be greater than zero")
        if target_status not in {
            MemoryAdmissionStatus.PENDING,
            MemoryAdmissionStatus.APPROVED,
            MemoryAdmissionStatus.REJECTED,
            MemoryAdmissionStatus.QUARANTINED,
        }:
            raise BadRequestError("Unsupported human admission decision")

        current = await self._require_admission(context.tenant_id, admission_id)
        example = await self._get_example(context.tenant_id, current.example_id)
        idempotency_hash = self._decision_idempotency_hash(
            context.tenant_id,
            "admission",
            key,
        )
        action = self._admission_action(target_status)
        decisions = await self._admissions.list_decisions(
            context.tenant_id,
            current.example_id,
        )
        replay = next(
            (
                item
                for item in decisions
                if item.idempotency_key_hash == idempotency_hash
            ),
            None,
        )
        if replay is not None:
            if (
                replay.status is not target_status
                or replay.reason != reason
                or replay.policy_version != self._admission_policy_version
            ):
                raise ResourceConflictError(
                    "Idempotency-Key is already bound to another admission decision"
                )
            return MemoryAdmissionDecisionResult(
                item=MemoryAdmissionGovernanceItem(current, example),
                decision=replay,
                projection_reconciled=await self._reconcile_admission_projection(
                    example,
                    current,
                ),
                audit_event=await self._governance.get_audit_by_idempotency_hash(
                    context.tenant_id,
                    action,
                    idempotency_hash,
                ),
            )
        if current.revision != expected_revision:
            raise ResourceConflictError("Memory admission revision is stale")
        if current.status is target_status:
            raise ResourceConflictError("Memory admission already has the requested status")
        self._validate_human_admission_transition(current.status, target_status)
        if (
            self._require_distinct_second_reviewer
            and context.actor_id == example.reviewer_id
        ):
            raise ResourceConflictError(
                "The second reviewer must differ from the original reviewer"
            )

        assessments = await self._admissions.list_assessments(
            context.tenant_id,
            current.example_id,
        )
        conflicts = await self._conflicts.list_open_for_example(
            context.tenant_id,
            current.example_id,
        )
        if target_status is MemoryAdmissionStatus.APPROVED:
            self._validate_human_approval(example, assessments, conflicts)
        now = datetime.now(UTC)
        decision = MemoryAdmissionDecision(
            decision_id=sha256(
                f"memory-admission-human-decision\0{idempotency_hash}".encode("utf-8")
            ).hexdigest(),
            tenant_id=context.tenant_id,
            example_id=current.example_id,
            previous_status=current.status,
            status=target_status,
            authority=MemoryAdmissionDecisionAuthority.HUMAN_GOVERNOR,
            decided_by=context.actor_id,
            reason=reason,
            reason_codes=(f"human_governance.{target_status.value}",),
            assessment_ids=tuple(item.assessment_id for item in assessments),
            conflict_ids=tuple(item.conflict_id for item in conflicts),
            policy_version=self._admission_policy_version,
            idempotency_key_hash=idempotency_hash,
            revision=current.revision + 1,
            decided_at=now,
        )
        audit_event = self._audit_event_from_hash(
            context,
            action,
            "memory_admission",
            current.example_id,
            reason,
            idempotency_hash,
            str(decision.revision),
            now,
        )
        try:
            updated = await self._admissions.append_decision(
                decision,
                expected_revision=current.revision,
                audit_event=audit_event,
            )
        except WorkflowPersistenceError as exc:
            raise ResourceConflictError(
                "Memory admission decision conflicts with current state"
            ) from exc
        persisted_audit = await self._governance.get_audit_by_idempotency_hash(
            context.tenant_id,
            action,
            idempotency_hash,
        )
        return MemoryAdmissionDecisionResult(
            item=MemoryAdmissionGovernanceItem(updated, example),
            decision=decision,
            projection_reconciled=await self._reconcile_admission_projection(
                example,
                updated,
            ),
            audit_event=persisted_audit,
        )

    async def decide_admissions_batch(
        self,
        context: TrustedTenantContext,
        target_status: MemoryAdmissionStatus,
        *,
        items: Sequence[tuple[str, int]],
        reason: str,
        idempotency_key: str | None,
    ) -> MemoryAdmissionBatchDecisionResult:
        """Apply one governed decision to a bounded, revision-pinned batch."""

        self._require(context, MemoryPermission.GOVERN)
        normalized_reason = self._normalize_reason(reason)
        root_key = normalize_idempotency_key(idempotency_key)
        commands = tuple(items)
        if not commands or len(commands) > 100:
            raise BadRequestError("Batch decisions require 1 to 100 admissions")
        identifiers = tuple(
            self._normalize_identifier("admission_id", admission_id)
            for admission_id, _revision in commands
        )
        if len(identifiers) != len(set(identifiers)):
            raise BadRequestError("Batch admission identifiers must be unique")

        outcomes: list[MemoryAdmissionBatchDecisionItem] = []
        for (_admission_id, expected_revision), normalized_id in zip(
            commands,
            identifiers,
            strict=True,
        ):
            item_key = sha256(
                f"memory-admission-batch\0{root_key}\0{normalized_id}".encode("utf-8")
            ).hexdigest()
            try:
                result = await self.decide_admission(
                    context,
                    normalized_id,
                    target_status,
                    reason=normalized_reason,
                    expected_revision=expected_revision,
                    idempotency_key=item_key,
                )
                outcomes.append(
                    MemoryAdmissionBatchDecisionItem(
                        admission_id=normalized_id,
                        result=result,
                    )
                )
            except ApplicationServiceError as exc:
                outcomes.append(
                    MemoryAdmissionBatchDecisionItem(
                        admission_id=normalized_id,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                )

        return MemoryAdmissionBatchDecisionResult(
            target_status=target_status,
            items=tuple(outcomes),
        )

    async def list_field_semantics(
        self,
        context: TrustedTenantContext,
        *,
        document_type: str | None,
        canonical_field_path: str | None,
        catalog_version: str | None,
        alias_statuses: Sequence[FieldAliasStatus],
        limit: int,
        cursor: str | None,
    ) -> FieldSemanticGovernancePage:
        self._require(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        if not 1 <= limit <= 100:
            raise BadRequestError("limit must be between 1 and 100")
        document_type = self._normalize_optional_identifier(
            "document_type",
            document_type,
        )
        canonical_field_path = self._normalize_optional_identifier(
            "canonical_field_path",
            canonical_field_path,
        )
        cursor = self._normalize_optional_identifier("cursor", cursor)
        version = (
            FieldSemanticCatalogVersion(
                self._normalize_identifier("catalog_version", catalog_version)
            )
            if catalog_version is not None
            else None
        )
        try:
            definitions = await self._field_semantics.list_definitions(
                context.tenant_id,
                document_type=document_type,
                catalog_version=version,
            )
        except ValueError as exc:
            raise BadRequestError("Field semantic catalog scope is invalid") from exc
        if canonical_field_path is not None:
            definitions = tuple(
                item
                for item in definitions
                if item.canonical_field_path == canonical_field_path
            )
        statuses = self._unique_values("field alias status", alias_statuses)
        candidates = await self._field_alias_candidates.list_for_governance(
            context.tenant_id,
            statuses,
            schema_version=self._schema_version,
            document_type=document_type,
            canonical_field_path=canonical_field_path,
            limit=limit + 1,
            after_candidate_id=cursor,
        )
        has_more = len(candidates) > limit
        page = candidates[:limit]
        items = tuple(
            [await self._field_alias_item(context.tenant_id, item) for item in page]
        )
        return FieldSemanticGovernancePage(
            definitions=definitions,
            alias_candidates=items,
            next_cursor=(items[-1].candidate.candidate_id if has_more and items else None),
        )

    async def list_field_semantic_conflicts(
        self,
        context: TrustedTenantContext,
        *,
        statuses: Sequence[MemoryConflictStatus],
        limit: int,
        cursor: str | None,
    ) -> FieldSemanticConflictPage:
        self._require(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        if not 1 <= limit <= 100:
            raise BadRequestError("limit must be between 1 and 100")
        cursor = self._normalize_optional_identifier("cursor", cursor)
        records = await self._conflicts.list_for_governance(
            context.tenant_id,
            self._unique_values("conflict status", statuses),
            alias_conflicts_only=True,
            limit=limit + 1,
            after_conflict_id=cursor,
        )
        has_more = len(records) > limit
        page = records[:limit]
        return FieldSemanticConflictPage(
            items=page,
            next_cursor=(page[-1].conflict_id if has_more and page else None),
        )

    async def resolve_field_semantic_conflict(
        self,
        context: TrustedTenantContext,
        conflict_id: str,
        *,
        expected_status: MemoryConflictStatus,
        reason: str,
        selected_canonical_field_path: str | None,
        resolution_note: str | None,
        idempotency_key: str | None,
    ) -> MemoryConflictResolutionResult:
        return await self._decide_field_semantic_conflict(
            context,
            conflict_id,
            MemoryConflictStatus.RESOLVED,
            expected_status=expected_status,
            reason=reason,
            selected_canonical_field_path=selected_canonical_field_path,
            resolution_note=resolution_note,
            idempotency_key=idempotency_key,
        )

    async def dismiss_field_semantic_conflict(
        self,
        context: TrustedTenantContext,
        conflict_id: str,
        *,
        expected_status: MemoryConflictStatus,
        reason: str,
        selected_canonical_field_path: str | None,
        resolution_note: str | None,
        idempotency_key: str | None,
    ) -> MemoryConflictResolutionResult:
        return await self._decide_field_semantic_conflict(
            context,
            conflict_id,
            MemoryConflictStatus.DISMISSED,
            expected_status=expected_status,
            reason=reason,
            selected_canonical_field_path=selected_canonical_field_path,
            resolution_note=resolution_note,
            idempotency_key=idempotency_key,
        )

    async def _decide_field_semantic_conflict(
        self,
        context: TrustedTenantContext,
        conflict_id: str,
        target_status: MemoryConflictStatus,
        *,
        expected_status: MemoryConflictStatus,
        reason: str,
        selected_canonical_field_path: str | None,
        resolution_note: str | None,
        idempotency_key: str | None,
    ) -> MemoryConflictResolutionResult:
        if not (
            context.permits(MemoryPermission.GOVERN)
            or context.permits(MemoryPermission.GOVERN_FIELD_ALIAS)
        ):
            raise ForbiddenError("Missing required conflict governance permission")
        conflict_id = self._normalize_identifier("conflict_id", conflict_id)
        reason = self._normalize_reason(reason)
        if len(reason) > 2_000:
            raise BadRequestError("reason exceeds 2000 characters")
        if expected_status is not MemoryConflictStatus.OPEN:
            raise BadRequestError("expected_status must be open")
        if target_status not in {
            MemoryConflictStatus.RESOLVED,
            MemoryConflictStatus.DISMISSED,
        }:
            raise BadRequestError("Unsupported conflict resolution status")
        selected_path = self._normalize_optional_identifier(
            "selected_canonical_field_path",
            selected_canonical_field_path,
        )
        sanitized_note = self._sanitize_resolution_note(resolution_note)
        key = normalize_idempotency_key(idempotency_key)

        conflict = await self._conflicts.get(context.tenant_id, conflict_id)
        if conflict is None:
            raise ResourceNotFoundError("Memory conflict was not found")
        self._require(
            context,
            (
                MemoryPermission.GOVERN_FIELD_ALIAS
                if conflict.field_alias_candidate_ids
                else MemoryPermission.GOVERN
            ),
        )
        idempotency_hash = self._decision_idempotency_hash(
            context.tenant_id,
            "memory-conflict-resolution",
            key,
        )
        action = (
            GovernanceAction.RESOLVE_CONFLICT
            if target_status is MemoryConflictStatus.RESOLVED
            else GovernanceAction.DISMISS_CONFLICT
        )
        replay = await self._conflicts.get_resolution_by_idempotency_hash(
            context.tenant_id,
            idempotency_hash,
        )
        if replay is not None:
            self._validate_conflict_resolution_replay(
                replay,
                context=context,
                conflict_id=conflict_id,
                target_status=target_status,
                reason=reason,
                selected_path=selected_path,
                resolution_note=sanitized_note,
            )
            if conflict.resolution_decision_id != replay.resolution_decision_id:
                raise ResourceConflictError(
                    "Conflict resolution replay no longer matches current state"
                )
            return MemoryConflictResolutionResult(
                conflict=conflict,
                decision=replay,
                reevaluation_registered=bool(replay.reevaluation_targets),
                audit_event=await self._governance.get_audit_by_idempotency_hash(
                    context.tenant_id,
                    action,
                    idempotency_hash,
                ),
            )
        if conflict.status is not expected_status:
            raise ResourceConflictError("Memory conflict status is stale")
        if self._require_distinct_second_reviewer:
            source_reviewers: set[str] = set()
            for example_id in conflict.example_ids:
                example = await self._get_example(context.tenant_id, example_id)
                source_reviewers.add(example.reviewer_id)
            for candidate_id in conflict.field_alias_candidate_ids:
                source_reviewers.update(
                    await self._field_alias_candidates.list_source_reviewer_ids(
                        context.tenant_id,
                        candidate_id,
                    )
                )
            if context.actor_id in source_reviewers:
                raise ResourceConflictError(
                    "The conflict reviewer must differ from the source reviewer"
                )
        is_field_mapping_conflict = bool(conflict.candidate_field_paths)
        if target_status is MemoryConflictStatus.RESOLVED:
            if is_field_mapping_conflict and selected_path is None:
                raise UnprocessableEntityError(
                    "Resolved field mapping conflicts require a selected candidate field"
                )
        elif selected_path is not None:
            raise UnprocessableEntityError(
                "Dismissed conflicts cannot select a canonical field path"
            )
        if selected_path is not None:
            if conflict.schema_version != self._schema_version:
                raise ResourceConflictError(
                    "Memory conflict Schema version is no longer current; dismiss it instead"
                )
            if selected_path not in conflict.candidate_field_paths:
                raise UnprocessableEntityError(
                    "Selected canonical field path is not a conflict candidate"
                )
            try:
                definition = await self._field_semantics.get_definition(
                    context.tenant_id,
                    conflict.document_type,
                    selected_path,
                )
            except ValueError as exc:
                raise UnprocessableEntityError(
                    "Current field semantic catalog cannot validate the selected path"
                ) from exc
            if (
                definition is None
                or not definition.is_valid
                or definition.schema_version != self._schema_version
            ):
                raise UnprocessableEntityError(
                    "Selected canonical field path is absent from the current Schema"
                )

        reevaluation_targets = tuple(
            sorted(
                (
                    *(
                        MemoryConflictReevaluationTarget(
                            MemoryConflictReevaluationTargetType.MEMORY_ADMISSION,
                            example_id,
                        )
                        for example_id in conflict.example_ids
                    ),
                    *(
                        MemoryConflictReevaluationTarget(
                            MemoryConflictReevaluationTargetType.FIELD_ALIAS,
                            candidate_id,
                        )
                        for candidate_id in conflict.field_alias_candidate_ids
                    ),
                ),
                key=lambda item: (item.target_type.value, item.target_id),
            )
        )
        now = datetime.now(UTC)
        decision = MemoryConflictResolutionDecision(
            resolution_decision_id=sha256(
                f"memory-conflict-resolution\0{idempotency_hash}".encode("utf-8")
            ).hexdigest(),
            tenant_id=context.tenant_id,
            conflict_id=conflict.conflict_id,
            previous_status=MemoryConflictStatus.OPEN,
            target_status=target_status,
            selected_canonical_field_path=selected_path,
            reviewer_id=context.actor_id,
            reason=reason,
            resolution_note=sanitized_note,
            idempotency_key_hash=idempotency_hash,
            policy_version=self._conflict_resolution_policy_version,
            decided_at=now,
            reevaluation_targets=reevaluation_targets,
        )
        audit_event = self._audit_event_from_hash(
            context,
            action,
            "memory_conflict",
            conflict.conflict_id,
            reason,
            idempotency_hash,
            decision.resolution_decision_id,
            now,
        )
        try:
            updated = await self._conflicts.resolve_with_decision(
                decision,
                audit_event,
            )
        except WorkflowPersistenceError as exc:
            concurrent = await self._conflicts.get_resolution_by_idempotency_hash(
                context.tenant_id,
                idempotency_hash,
            )
            if concurrent is not None:
                self._validate_conflict_resolution_replay(
                    concurrent,
                    context=context,
                    conflict_id=conflict_id,
                    target_status=target_status,
                    reason=reason,
                    selected_path=selected_path,
                    resolution_note=sanitized_note,
                )
                latest = await self._conflicts.get(context.tenant_id, conflict_id)
                if (
                    latest is not None
                    and latest.resolution_decision_id
                    == concurrent.resolution_decision_id
                ):
                    return MemoryConflictResolutionResult(
                        conflict=latest,
                        decision=concurrent,
                        reevaluation_registered=bool(
                            concurrent.reevaluation_targets
                        ),
                        audit_event=(
                            await self._governance.get_audit_by_idempotency_hash(
                                context.tenant_id,
                                action,
                                idempotency_hash,
                            )
                        ),
                    )
            raise ResourceConflictError(
                "Memory conflict was resolved concurrently"
            ) from exc
        persisted_audit = await self._governance.get_audit_by_idempotency_hash(
            context.tenant_id,
            action,
            idempotency_hash,
        )
        return MemoryConflictResolutionResult(
            conflict=updated,
            decision=decision,
            reevaluation_registered=bool(reevaluation_targets),
            audit_event=persisted_audit,
        )

    async def approve_field_alias(
        self,
        context: TrustedTenantContext,
        alias_id: str,
        reason: str,
        expected_revision: int,
        idempotency_key: str | None,
    ) -> FieldAliasDecisionResult:
        self._require(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        alias_id = self._normalize_identifier("alias_id", alias_id)
        reason = self._normalize_reason(reason)
        if expected_revision <= 0:
            raise BadRequestError("expected_revision must be greater than zero")
        key = normalize_idempotency_key(idempotency_key)
        await self._require_field_alias_candidate(
            context.tenant_id,
            alias_id,
        )
        source_reviewers = await self._field_alias_candidates.list_source_reviewer_ids(
            context.tenant_id,
            alias_id,
        )
        if (
            self._require_distinct_second_reviewer
            and context.actor_id in source_reviewers
        ):
            raise ResourceConflictError(
                "The alias approver must differ from every source mapping reviewer"
            )
        idempotency_hash = sha256(key.encode("utf-8")).hexdigest()
        action = GovernanceAction.APPROVE_FIELD_ALIAS
        audit_event = self._audit_event_from_hash(
            context,
            action,
            "field_alias_candidate",
            alias_id,
            reason,
            idempotency_hash,
            str(expected_revision + 1),
            datetime.now(UTC),
        )
        try:
            promotion = await self._field_alias_learning.approve_tenant_candidate(
                context,
                alias_id,
                reason,
                key,
                expected_revision,
                audit_event,
            )
        except WorkflowPersistenceError as exc:
            raise ResourceConflictError(
                "Field alias approval conflicts with current state"
            ) from exc
        return FieldAliasDecisionResult(
            item=await self._field_alias_item(context.tenant_id, promotion.candidate),
            promoted_catalog_version=promotion.catalog_version,
            audit_event=await self._governance.get_audit_by_idempotency_hash(
                context.tenant_id,
                action,
                idempotency_hash,
            ),
        )

    async def disable_field_alias(
        self,
        context: TrustedTenantContext,
        alias_id: str,
        reason: str,
        expected_revision: int,
        idempotency_key: str | None,
    ) -> FieldAliasDecisionResult:
        self._require(context, MemoryPermission.GOVERN_FIELD_ALIAS)
        alias_id = self._normalize_identifier("alias_id", alias_id)
        reason = self._normalize_reason(reason)
        if expected_revision <= 0:
            raise BadRequestError("expected_revision must be greater than zero")
        key = normalize_idempotency_key(idempotency_key)
        await self._require_field_alias_candidate(context.tenant_id, alias_id)
        idempotency_hash = sha256(key.encode("utf-8")).hexdigest()
        action = GovernanceAction.DISABLE_FIELD_ALIAS
        audit_event = self._audit_event_from_hash(
            context,
            action,
            "field_alias_candidate",
            alias_id,
            reason,
            idempotency_hash,
            str(expected_revision + 1),
            datetime.now(UTC),
        )
        try:
            candidate = await self._field_alias_learning.disable_tenant_alias(
                context,
                alias_id,
                reason,
                key,
                expected_revision,
                audit_event,
            )
        except WorkflowPersistenceError as exc:
            raise ResourceConflictError(
                "Field alias disable operation conflicts with current state"
            ) from exc
        return FieldAliasDecisionResult(
            item=await self._field_alias_item(context.tenant_id, candidate),
            promoted_catalog_version=candidate.promoted_catalog_version,
            audit_event=await self._governance.get_audit_by_idempotency_hash(
                context.tenant_id,
                action,
                idempotency_hash,
            ),
        )

    async def disable_example(
        self,
        context: TrustedTenantContext,
        example_id: str,
        reason: str,
        idempotency_key: str | None,
    ) -> GovernanceOperationResult:
        self._require(context, MemoryPermission.GOVERN)
        example_id = self._normalize_identifier("example_id", example_id)
        normalized_reason = self._normalize_reason(reason)
        action = GovernanceAction.DISABLE_EXAMPLE
        key, fingerprint, operation = await self._claim(
            context,
            action,
            idempotency_key,
            {"example_id": example_id, "reason": normalized_reason},
        )
        completed = await self._completed_result(operation, key, fingerprint)
        if completed is not None:
            return completed
        recovered = await self._recover_governance_operation(
            context,
            action,
            operation,
            key,
            fingerprint,
        )
        if recovered is not None:
            return recovered
        example = await self._get_example(context.tenant_id, example_id)
        versions = await self._examples.list_index_versions(
            context.tenant_id,
            schema_version=example.schema_version,
        )
        if versions and self._projection_service is None:
            raise ServiceUnavailableError(
                "Example index cleanup is unavailable for registered index versions"
            )
        audit_event = self._audit_event(
            context,
            action,
            "reviewed_example",
            example_id,
            normalized_reason,
            key,
            datetime.now(UTC),
            resource_version=None,
        )
        if self._projection_service is not None and versions:
            changed = await self._projection_service.invalidate_example(
                context.tenant_id,
                example_id,
                versions,
                normalized_reason,
                audit_event,
            )
        else:
            changed = await self._examples.invalidate(
                context.tenant_id,
                example_id,
                normalized_reason,
                audit_event,
            )
        result = self._governance_operation_result(
            audit_event,
            1 if changed else 0,
        )
        await self._complete(operation, key, fingerprint, result)
        return result

    async def invalidate_schema(
        self,
        context: TrustedTenantContext,
        schema_version: str,
        reason: str,
        idempotency_key: str | None,
    ) -> GovernanceOperationResult:
        self._require(context, MemoryPermission.GOVERN)
        schema_version = self._normalize_identifier("schema_version", schema_version)
        normalized_reason = self._normalize_reason(reason)
        action = GovernanceAction.INVALIDATE_SCHEMA
        key, fingerprint, operation = await self._claim(
            context,
            action,
            idempotency_key,
            {"schema_version": schema_version, "reason": normalized_reason},
        )
        completed = await self._completed_result(operation, key, fingerprint)
        if completed is not None:
            return completed
        recovered = await self._recover_governance_operation(
            context,
            action,
            operation,
            key,
            fingerprint,
        )
        if recovered is not None:
            return recovered
        versions = await self._examples.list_index_versions(
            context.tenant_id,
            schema_version=schema_version,
        )
        if versions and self._projection_service is None:
            raise ServiceUnavailableError(
                "Schema index cleanup is unavailable for registered index versions"
            )
        audit_event = self._audit_event(
            context,
            action,
            "schema_version",
            schema_version,
            normalized_reason,
            key,
            datetime.now(UTC),
            resource_version=schema_version,
        )
        if self._projection_service is not None and versions:
            count = await self._projection_service.invalidate_schema(
                context.tenant_id,
                schema_version,
                versions,
                normalized_reason,
                audit_event,
            )
        else:
            count = await self._examples.invalidate_schema(
                context.tenant_id,
                schema_version,
                normalized_reason,
                audit_event,
            )
        result = self._governance_operation_result(audit_event, count)
        await self._complete(operation, key, fingerprint, result)
        return result

    async def rebuild_index(
        self,
        context: TrustedTenantContext,
        spec: IndexRebuildSpec,
        idempotency_key: str | None,
    ) -> MemoryIndexView:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        if self._projection_service is None:
            raise ServiceUnavailableError("Example index projection is not configured")
        action = GovernanceAction.REBUILD_INDEX
        request_payload: dict[str, object] = {
            "index_version": spec.index_version.value,
            "schema_version": spec.schema_version,
            "dense_model_version": spec.dense_model_version.value,
            "sparse_model_version": (
                spec.sparse_model_version.value if spec.sparse_model_version else None
            ),
            "rerank_model_version": (
                spec.rerank_model_version.value if spec.rerank_model_version else None
            ),
            "prompt_version": spec.prompt_version.value,
            "reason": spec.reason,
        }
        key, fingerprint, operation = await self._claim(
            context,
            action,
            idempotency_key,
            request_payload,
        )
        claim = await self._idempotency.get_idempotency(operation, key)
        if claim is not None and claim.status is IdempotencyStatus.COMPLETED:
            audit_event = await self._governance.get_audit_by_idempotency_hash(
                context.tenant_id,
                action,
                self._audit_idempotency_hash(context, action, key),
            )
            return await self._get_index_view(
                context.tenant_id,
                spec.index_version,
                audit_event=audit_event,
            )
        recovered_audit = await self._governance.get_audit_by_idempotency_hash(
            context.tenant_id,
            action,
            self._audit_idempotency_hash(context, action, key),
        )
        if recovered_audit is not None:
            recovered = self._governance_operation_result(recovered_audit, 0)
            await self._complete(operation, key, fingerprint, recovered)
            return await self._get_index_view(
                context.tenant_id,
                spec.index_version,
                audit_event=recovered_audit,
            )
        existing = await self._examples.get_index_governance(
            context.tenant_id,
            spec.index_version,
        )
        if existing is not None and not self._same_index_definition(existing, spec):
            raise ResourceConflictError(
                "Index version is already bound to different immutable configuration"
            )
        audit_event = self._audit_event(
            context,
            action,
            "index_version",
            spec.index_version.value,
            spec.reason,
            key,
            datetime.now(UTC),
            resource_version=spec.index_version.value,
        )
        await self._projection_service.register_index_version(
            context.tenant_id,
            spec.index_version,
            spec.schema_version,
            spec.dense_model_version,
            spec.sparse_model_version,
            spec.rerank_model_version,
            spec.prompt_version,
            audit_event,
        )
        result = self._governance_operation_result(audit_event, 1)
        await self._complete(operation, key, fingerprint, result)
        return await self._get_index_view(
            context.tenant_id,
            spec.index_version,
            audit_event=audit_event,
        )

    async def get_index(
        self,
        context: TrustedTenantContext,
        index_version: str,
    ) -> MemoryIndexView:
        self._require(context, MemoryPermission.READ)
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        return await self._get_index_view(context.tenant_id, version)

    async def project_index(
        self,
        context: TrustedTenantContext,
        index_version: str,
        *,
        limit: int | None = None,
    ) -> IndexProjectionExecution:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        if self._projection_service is None:
            raise ServiceUnavailableError("Example index projection is not configured")
        if limit is not None and not 1 <= limit <= 500:
            raise BadRequestError("Projection limit must be between 1 and 500")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        await self._get_index_view(context.tenant_id, version)
        batch = await self._projection_service.project_pending(
            context.tenant_id,
            version,
            limit=limit,
            trace_id=context.trace_id,
        )
        return IndexProjectionExecution(
            batch=batch,
            index=await self._get_index_view(context.tenant_id, version),
        )

    async def activate_index(
        self,
        context: TrustedTenantContext,
        index_version: str,
    ) -> MemoryIndexView:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        if self._projection_service is None:
            raise ServiceUnavailableError("Example index projection is not configured")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        try:
            await self._projection_service.activate_index_version(
                context.tenant_id,
                version,
            )
        except WorkflowPersistenceError as exc:
            raise ResourceConflictError(str(exc)) from exc
        return await self._get_index_view(context.tenant_id, version)

    async def rollback_index(self, context: TrustedTenantContext, index_version: str) -> MemoryIndexView:
        self._require(context, MemoryPermission.REBUILD_INDEX)
        if self._projection_service is None:
            raise ServiceUnavailableError("Example index projection is not configured")
        version = IndexVersion(self._normalize_identifier("index_version", index_version))
        target = await self._projection_service.get_index_governance(context.tenant_id, version)
        if target is None or not target.is_valid or target.projection_counts.pending or target.projection_counts.processing or target.projection_counts.failed:
            raise ResourceConflictError("Rollback target is not a fully verified valid index")
        try:
            await self._projection_service.activate_index_version(context.tenant_id, version)
        except WorkflowPersistenceError as exc:
            raise ResourceConflictError(str(exc)) from exc
        return await self._get_index_view(context.tenant_id, version)

    async def _get_index_view(
        self,
        tenant_id: str,
        version: IndexVersion,
        *,
        audit_event: GovernanceAuditEvent | None = None,
    ) -> MemoryIndexView:
        index = await self._examples.get_index_governance(tenant_id, version)
        if index is None:
            raise ResourceNotFoundError("Index version was not found")
        metrics = await self._telemetry.summarize(tenant_id, version.value)
        return MemoryIndexView(index=index, metrics=metrics, audit_event=audit_event)

    async def submit_feedback(
        self,
        context: TrustedTenantContext,
        *,
        trace_id: str,
        example_id: str,
        label: RetrievalFeedbackLabel,
        reason: str | None,
        idempotency_key: str | None,
    ) -> MemoryFeedbackResult:
        self._require(context, MemoryPermission.SUBMIT_FEEDBACK)
        trace_id = self._normalize_identifier("trace_id", trace_id)
        example_id = self._normalize_identifier("example_id", example_id)
        normalized_reason = self._normalize_optional_reason(reason)
        action = GovernanceAction.SUBMIT_FEEDBACK
        key, fingerprint, operation = await self._claim(
            context,
            action,
            idempotency_key,
            {
                "trace_id": trace_id,
                "example_id": example_id,
                "label": label.value,
                "reason": normalized_reason,
            },
        )
        claim = await self._idempotency.get_idempotency(operation, key)
        if claim is not None and claim.status is IdempotencyStatus.COMPLETED:
            response = claim.response_payload or {}
            feedback_id = response.get("feedback_id")
            if not isinstance(feedback_id, str):
                raise ResourceConflictError("Completed feedback response is invalid")
            existing = await self._governance.get_feedback(context.tenant_id, feedback_id)
            if existing is None:
                raise ResourceConflictError("Completed feedback record is missing")
            audit_hash = self._audit_idempotency_hash(context, action, key)
            return MemoryFeedbackResult(
                feedback=existing,
                audit_event=await self._governance.get_audit_by_idempotency_hash(
                    context.tenant_id,
                    action,
                    audit_hash,
                ),
            )
        recovered_audit = await self._governance.get_audit_by_idempotency_hash(
            context.tenant_id,
            action,
            self._audit_idempotency_hash(context, action, key),
        )
        if recovered_audit is not None:
            existing = await self._governance.get_feedback(
                context.tenant_id,
                recovered_audit.resource_id,
            )
            if existing is None:
                raise ResourceConflictError("Governance audit references missing feedback")
            await self._idempotency.complete_idempotency(
                operation,
                key,
                fingerprint,
                {"feedback_id": existing.feedback_id},
            )
            return MemoryFeedbackResult(
                feedback=existing,
                audit_event=recovered_audit,
            )
        trace = await self._telemetry.get_trace(context.tenant_id, trace_id)
        if trace is None:
            raise ResourceNotFoundError("Retrieval trace was not found")
        if example_id not in set(trace.positive_example_ids + trace.negative_example_ids):
            raise ResourceConflictError("Example was not returned by the retrieval trace")
        await self._get_example(context.tenant_id, example_id)
        now = datetime.now(UTC)
        feedback_id = sha256(
            f"memory-feedback\0{context.tenant_id}\0{fingerprint}".encode("utf-8")
        ).hexdigest()
        feedback_record = RetrievalFeedback(
            feedback_id=feedback_id,
            tenant_id=context.tenant_id,
            trace_id=trace_id,
            example_id=example_id,
            label=label,
            reviewer_id=context.actor_id,
            reason=normalized_reason,
            created_at=now,
        )
        audit_event = self._audit_event(
            context,
            action,
            "retrieval_feedback",
            feedback_record.feedback_id,
            normalized_reason or "confirmed_helpful",
            key,
            now,
            resource_version=None,
        )
        feedback = await self._governance.save_feedback(
            feedback_record,
            audit_event,
        )
        await self._idempotency.complete_idempotency(
            operation,
            key,
            fingerprint,
            {"feedback_id": feedback.feedback_id},
        )
        return MemoryFeedbackResult(feedback=feedback, audit_event=audit_event)

    async def get_evaluation(
        self,
        context: TrustedTenantContext,
        evaluation_run_id: str,
    ) -> EvaluationRun:
        self._require(context, MemoryPermission.READ_EVALUATION)
        evaluation_run_id = self._normalize_identifier(
            "evaluation_run_id",
            evaluation_run_id,
        )
        run = await self._evaluations.get_run(context.tenant_id, evaluation_run_id)
        if run is None:
            raise ResourceNotFoundError("Evaluation run was not found")
        return run

    async def _require_admission(
        self,
        tenant_id: str,
        admission_id: str,
    ) -> MemoryAdmissionRecord:
        admission = await self._admissions.get(tenant_id, admission_id)
        if admission is None:
            raise ResourceNotFoundError("Memory admission was not found")
        return admission

    async def _admission_item(
        self,
        tenant_id: str,
        admission: MemoryAdmissionRecord,
    ) -> MemoryAdmissionGovernanceItem:
        return MemoryAdmissionGovernanceItem(
            admission=admission,
            example=await self._get_example(tenant_id, admission.example_id),
        )

    @staticmethod
    def _validate_human_admission_transition(
        current: MemoryAdmissionStatus,
        target: MemoryAdmissionStatus,
    ) -> None:
        allowed = {
            MemoryAdmissionStatus.PENDING: frozenset(
                {
                    MemoryAdmissionStatus.APPROVED,
                    MemoryAdmissionStatus.REJECTED,
                    MemoryAdmissionStatus.QUARANTINED,
                }
            ),
            MemoryAdmissionStatus.QUARANTINED: frozenset(
                {
                    MemoryAdmissionStatus.PENDING,
                    MemoryAdmissionStatus.APPROVED,
                    MemoryAdmissionStatus.REJECTED,
                }
            ),
            MemoryAdmissionStatus.SUSPENDED: frozenset(
                {MemoryAdmissionStatus.APPROVED}
            ),
            MemoryAdmissionStatus.APPROVED: frozenset(),
            MemoryAdmissionStatus.REJECTED: frozenset(),
            MemoryAdmissionStatus.INVALIDATED: frozenset(),
        }
        if target not in allowed[current]:
            raise ResourceConflictError(
                f"Memory admission cannot transition from {current.value} to {target.value}"
            )

    def _validate_human_approval(
        self,
        example: ReviewedExample,
        assessments: Sequence[MemoryQualityAssessment],
        conflicts: Sequence[MemoryConflictRecord],
    ) -> None:
        if not example.is_reviewed or not example.is_valid:
            raise ResourceConflictError(
                "Only reviewed and valid examples may be approved"
            )
        deterministic = tuple(
            item
            for item in assessments
            if item.source is MemoryAssessmentSource.DETERMINISTIC
            and item.policy_version == self._admission_policy_version
        )
        latest = max(
            deterministic,
            key=lambda item: (item.created_at, item.assessment_id),
            default=None,
        )
        if latest is None:
            raise ResourceConflictError(
                "A current deterministic quality assessment is required before approval"
            )
        if any(signal.verdict is SignalVerdict.FAILED for signal in latest.signals):
            raise ResourceConflictError(
                "Deterministic quality failures cannot be overridden by human approval"
            )
        if any(item.status is MemoryConflictStatus.OPEN for item in conflicts):
            raise ResourceConflictError(
                "Open memory conflicts must be resolved before approval"
            )

    async def _reconcile_admission_projection(
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
            else:
                await self._projection_service.remove_derived_example(
                    example.tenant_id,
                    example.example_id,
                    example.schema_version,
                )
            return True
        except Exception as exc:
            _LOGGER.error(
                "memory_governance_projection_reconciliation_failed",
                extra={
                    "tenant_id": example.tenant_id,
                    "example_id": example.example_id,
                    "admission_status": admission.status.value,
                    "error_type": type(exc).__name__,
                },
            )
            return False

    async def _require_field_alias_candidate(
        self,
        tenant_id: str,
        candidate_id: str,
    ) -> FieldAliasCandidate:
        candidate = await self._field_alias_candidates.get(tenant_id, candidate_id)
        if candidate is None:
            raise ResourceNotFoundError("Field alias candidate was not found")
        return candidate

    async def _field_alias_item(
        self,
        tenant_id: str,
        candidate: FieldAliasCandidate,
    ) -> FieldAliasGovernanceItem:
        now = datetime.now(UTC)
        support = await self._field_alias_candidates.summarize_support(
            tenant_id,
            candidate.candidate_id,
            now,
        )
        reviewers = await self._field_alias_candidates.list_source_reviewer_ids(
            tenant_id,
            candidate.candidate_id,
        )
        conflicts = await self._conflicts.list_open_for_alias_candidate(
            tenant_id,
            candidate.candidate_id,
        )
        return FieldAliasGovernanceItem(
            candidate=candidate,
            support=support,
            source_reviewer_count=len(reviewers),
            conflict_ids=tuple(item.conflict_id for item in conflicts),
        )

    @staticmethod
    def _decision_idempotency_hash(
        tenant_id: str,
        namespace: str,
        key: str,
    ) -> str:
        return sha256(f"{tenant_id}\0{namespace}\0{key}".encode("utf-8")).hexdigest()

    def _validate_conflict_resolution_replay(
        self,
        replay: MemoryConflictResolutionDecision,
        *,
        context: TrustedTenantContext,
        conflict_id: str,
        target_status: MemoryConflictStatus,
        reason: str,
        selected_path: str | None,
        resolution_note: str | None,
    ) -> None:
        if (
            replay.conflict_id != conflict_id
            or replay.target_status is not target_status
            or replay.reviewer_id != context.actor_id
            or replay.reason != reason
            or replay.selected_canonical_field_path != selected_path
            or replay.resolution_note != resolution_note
        ):
            raise ResourceConflictError(
                "Idempotency-Key is already bound to another conflict decision"
            )

    @staticmethod
    def _sanitize_resolution_note(note: str | None) -> str | None:
        if note is None:
            return None
        normalized = " ".join(unicodedata.normalize("NFKC", note).split())
        if not normalized:
            raise BadRequestError("resolution_note must not be blank")
        if len(normalized) > 2_000:
            raise BadRequestError("resolution_note exceeds 2000 characters")
        if _DATA_IMAGE_RE.search(normalized) or _BASE64_BLOCK_RE.search(normalized):
            raise UnprocessableEntityError(
                "resolution_note must not contain image or Base64 payloads"
            )
        normalized = _EMAIL_RE.sub("[REDACTED_EMAIL]", normalized)
        normalized = _PHONE_RE.sub("[REDACTED_NUMBER]", normalized)
        normalized = _LONG_NUMBER_RE.sub("[REDACTED_NUMBER]", normalized)
        return normalized

    @staticmethod
    def _unique_values(name: str, values: Sequence[_EnumT]) -> tuple[_EnumT, ...]:
        normalized = tuple(dict.fromkeys(values))
        if not normalized:
            raise BadRequestError(f"At least one {name} is required")
        return normalized

    async def _claim(
        self,
        context: TrustedTenantContext,
        action: GovernanceAction,
        idempotency_key: str | None,
        payload: dict[str, object],
    ) -> tuple[str, str, str]:
        key = normalize_idempotency_key(idempotency_key)
        fingerprint = sha256(
            self._canonical(
                {"tenant_id": context.tenant_id, "action": action.value, **payload}
            ).encode("utf-8")
        ).hexdigest()
        tenant_namespace = sha256(context.tenant_id.encode("utf-8")).hexdigest()[:24]
        operation = f"memory.{action.value}.{tenant_namespace}"
        resource_id = str(uuid4())
        claim = await self._idempotency.claim_idempotency(
            operation,
            key,
            fingerprint,
            resource_id,
        )
        if (
            claim.status is IdempotencyStatus.IN_PROGRESS
            and claim.resource_id != resource_id
        ):
            audit = await self._governance.get_audit_by_idempotency_hash(
                context.tenant_id,
                action,
                self._audit_idempotency_hash(context, action, key),
            )
            if audit is None:
                raise IdempotencyInProgressError(
                    "An equivalent memory governance operation is already in progress"
                )
        return key, fingerprint, operation

    async def _recover_governance_operation(
        self,
        context: TrustedTenantContext,
        action: GovernanceAction,
        operation: str,
        key: str,
        fingerprint: str,
    ) -> GovernanceOperationResult | None:
        audit = await self._governance.get_audit_by_idempotency_hash(
            context.tenant_id,
            action,
            self._audit_idempotency_hash(context, action, key),
        )
        if audit is None:
            return None
        result = self._governance_operation_result(audit, 0)
        await self._complete(operation, key, fingerprint, result)
        return result

    async def _completed_result(
        self,
        operation: str,
        key: str,
        fingerprint: str,
    ) -> GovernanceOperationResult | None:
        record = await self._idempotency.get_idempotency(operation, key)
        if record is None or record.status is not IdempotencyStatus.COMPLETED:
            return None
        if record.request_hash != fingerprint or record.response_payload is None:
            raise ResourceConflictError("Completed idempotency record is inconsistent")
        payload = record.response_payload
        try:
            changed_count_value = payload["changed_count"]
            if isinstance(changed_count_value, bool) or not isinstance(
                changed_count_value,
                int,
            ):
                raise TypeError("changed_count is not an integer")
            return GovernanceOperationResult(
                action=GovernanceAction(str(payload["action"])),
                resource_id=str(payload["resource_id"]),
                changed_count=changed_count_value,
                audit_id=str(payload["audit_id"]),
                resource_version=(
                    str(payload["resource_version"])
                    if payload.get("resource_version") is not None
                    else None
                ),
                trace_id=(
                    str(payload["trace_id"])
                    if payload.get("trace_id") is not None
                    else None
                ),
                performed_at=datetime.fromisoformat(str(payload["performed_at"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ResourceConflictError("Completed governance response is invalid") from exc

    @staticmethod
    def _governance_operation_result(
        event: GovernanceAuditEvent,
        changed_count: int,
    ) -> GovernanceOperationResult:
        return GovernanceOperationResult(
            action=event.action,
            resource_id=event.resource_id,
            changed_count=changed_count,
            audit_id=event.audit_id,
            resource_version=event.resource_version,
            trace_id=event.trace_id,
            performed_at=event.created_at,
        )

    async def _complete(
        self,
        operation: str,
        key: str,
        fingerprint: str,
        result: GovernanceOperationResult,
    ) -> None:
        await self._idempotency.complete_idempotency(
            operation,
            key,
            fingerprint,
            {
                "action": result.action.value,
                "resource_id": result.resource_id,
                "changed_count": result.changed_count,
                "audit_id": result.audit_id,
                "resource_version": result.resource_version,
                "trace_id": result.trace_id,
                "performed_at": result.performed_at.isoformat(),
            },
        )

    @staticmethod
    def _audit_event(
        context: TrustedTenantContext,
        action: GovernanceAction,
        resource_type: str,
        resource_id: str,
        reason: str,
        key: str,
        now: datetime,
        *,
        resource_version: str | None,
    ) -> GovernanceAuditEvent:
        key_hash = MemoryGovernanceService._audit_idempotency_hash(
            context,
            action,
            key,
        )
        return MemoryGovernanceService._audit_event_from_hash(
            context,
            action,
            resource_type,
            resource_id,
            reason,
            key_hash,
            resource_version,
            now,
        )

    @staticmethod
    def _audit_event_from_hash(
        context: TrustedTenantContext,
        action: GovernanceAction,
        resource_type: str,
        resource_id: str,
        reason: str,
        key_hash: str,
        resource_version: str | None,
        now: datetime,
    ) -> GovernanceAuditEvent:
        audit_id = sha256(
            f"memory-audit\0{context.tenant_id}\0{action.value}\0{key_hash}".encode(
                "utf-8"
            )
        ).hexdigest()
        return GovernanceAuditEvent(
            audit_id=audit_id,
            tenant_id=context.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            reviewer_id=context.actor_id,
            reason=MemoryGovernanceService._sanitize_audit_reason(reason),
            resource_version=resource_version,
            trace_id=context.trace_id or uuid4().hex,
            idempotency_key_hash=key_hash,
            created_at=now,
        )

    @staticmethod
    def _sanitize_audit_reason(reason: str) -> str:
        normalized = " ".join(unicodedata.normalize("NFKC", reason).split())
        if not normalized:
            raise BadRequestError("reason must not be blank")
        if len(normalized) > 2_000:
            raise BadRequestError("reason exceeds 2000 characters")
        if _DATA_IMAGE_RE.search(normalized) or _BASE64_BLOCK_RE.search(normalized):
            raise UnprocessableEntityError(
                "reason must not contain image or Base64 payloads"
            )
        normalized = _EMAIL_RE.sub("[REDACTED_EMAIL]", normalized)
        normalized = _PHONE_RE.sub("[REDACTED_NUMBER]", normalized)
        return _LONG_NUMBER_RE.sub("[REDACTED_NUMBER]", normalized)

    @staticmethod
    def _audit_idempotency_hash(
        context: TrustedTenantContext,
        action: GovernanceAction,
        key: str,
    ) -> str:
        return sha256(
            f"{context.tenant_id}\0{action.value}\0{key}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _admission_action(status: MemoryAdmissionStatus) -> GovernanceAction:
        return {
            MemoryAdmissionStatus.PENDING: GovernanceAction.REQUEUE_ADMISSION,
            MemoryAdmissionStatus.APPROVED: GovernanceAction.APPROVE_ADMISSION,
            MemoryAdmissionStatus.REJECTED: GovernanceAction.REJECT_ADMISSION,
            MemoryAdmissionStatus.QUARANTINED: GovernanceAction.QUARANTINE_ADMISSION,
        }[status]

    @staticmethod
    def _same_index_definition(
        index: IndexGovernanceRecord,
        spec: IndexRebuildSpec,
    ) -> bool:
        return (
            index.schema_version == spec.schema_version
            and index.dense_model_version == spec.dense_model_version
            and index.sparse_model_version == spec.sparse_model_version
            and index.rerank_model_version == spec.rerank_model_version
            and index.prompt_version == spec.prompt_version
            and index.is_valid
        )

    @staticmethod
    def _require(
        context: TrustedTenantContext,
        permission: MemoryPermission,
    ) -> None:
        if not context.permits(permission):
            raise ForbiddenError(f"Missing required permission: {permission.value}")

    @staticmethod
    def _normalize_reason(reason: str) -> str:
        normalized = reason.strip()
        if not normalized:
            raise BadRequestError("reason must not be blank")
        return normalized

    @classmethod
    def _normalize_optional_reason(cls, reason: str | None) -> str | None:
        return cls._normalize_reason(reason) if reason is not None else None

    @staticmethod
    def _normalize_identifier(name: str, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise BadRequestError(f"{name} must not be blank")
        return normalized

    @classmethod
    def _normalize_optional_identifier(
        cls,
        name: str,
        value: str | None,
    ) -> str | None:
        return cls._normalize_identifier(name, value) if value is not None else None

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
