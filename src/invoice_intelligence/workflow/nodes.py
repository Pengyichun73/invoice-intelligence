"""Idempotent nodes for the deterministic invoice workflow."""

import logging
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from invoice_intelligence.application.errors import (
    HumanCorrectionError,
    VisionExtractionError,
    WorkflowIdentityError,
    WorkflowPersistenceError,
)
from invoice_intelligence.application.ports.examples import ReviewedExampleContextProvider
from invoice_intelligence.application.ports.governance import RetrievalTelemetryRepository
from invoice_intelligence.application.ports.memory import (
    CorrectionMemoryRepository,
    MemoryRecoveryStatus,
)
from invoice_intelligence.application.ports.observability import PrivacyTelemetry
from invoice_intelligence.application.ports.workflow import (
    ExtractionResultRepository,
    ExtractionStateCodec,
    ExtractionValidator,
    HumanCorrectionApplier,
    ReviewTaskRepository,
    WorkflowRunRepository,
)
from invoice_intelligence.application.services.field_semantic_binding import (
    FieldSemanticBindingService,
)
from invoice_intelligence.application.services.memory_admission import (
    MemoryAdmissionService,
)
from invoice_intelligence.application.services.vision_extraction import VisionExtractionService
from invoice_intelligence.domain.extraction import (
    ExtractionAnomaly,
    ExtractionResult,
    VisionPromptContext,
)
from invoice_intelligence.domain.field_semantics import (
    FieldBindingEvidence,
    FieldBindingStatus,
)
from invoice_intelligence.domain.workflow import (
    FieldDecision,
    HumanCorrection,
    ReviewEvidenceSummary,
    ReviewField,
    ReviewRequest,
    SignalVerdict,
    ValidationIssue,
    ValidationOutcome,
    ValidationRoute,
    WorkflowIdentity,
    WorkflowStatus,
)
from invoice_intelligence.workflow.state import (
    GraphState,
    correction_events_from_state,
    correction_events_to_state,
    document_from_state,
    field_decisions_to_state,
    human_correction_from_state,
    human_correction_to_state,
    identity_from_state,
    issues_to_state,
    review_request_from_state,
    review_request_to_state,
    reviewed_example_context_from_state,
    reviewed_example_context_to_state,
    trace_id_from_state,
)

InvoiceT = TypeVar("InvoiceT")
StateUpdate = dict[str, object]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkflowDependencies(Generic[InvoiceT]):
    """Application boundaries injected into graph nodes."""

    output_schema: type[InvoiceT]
    extraction_service: VisionExtractionService[InvoiceT]
    extraction_validator: ExtractionValidator
    extraction_codec: ExtractionStateCodec
    correction_applier: HumanCorrectionApplier
    review_repository: ReviewTaskRepository
    run_repository: WorkflowRunRepository
    result_repository: ExtractionResultRepository
    correction_memory_repository: CorrectionMemoryRepository
    memory_admission_service: MemoryAdmissionService[InvoiceT]
    field_semantic_binding_service: FieldSemanticBindingService[InvoiceT] | None = None
    reviewed_example_context_provider: ReviewedExampleContextProvider | None = None
    retrieval_telemetry_repository: RetrievalTelemetryRepository | None = None
    correction_context_limit: int = 20
    memory_recovery_lease_seconds: float = 300.0
    privacy_telemetry: PrivacyTelemetry | None = None

    def __post_init__(self) -> None:
        if self.correction_context_limit <= 0:
            raise ValueError("correction_context_limit must be greater than zero")
        if self.memory_recovery_lease_seconds <= 0:
            raise ValueError("memory_recovery_lease_seconds must be greater than zero")


class InvoiceWorkflowNodes(Generic[InvoiceT]):
    """Node implementation with deterministic routing inputs and idempotent writes."""

    def __init__(self, dependencies: WorkflowDependencies[InvoiceT]) -> None:
        self._dependencies = dependencies

    async def prepare_document(
        self,
        state: GraphState,
        config: RunnableConfig,
    ) -> StateUpdate:
        try:
            configurable = config.get("configurable", {})
            runtime_thread_id = configurable.get("thread_id")
            if runtime_thread_id != state["thread_id"]:
                raise WorkflowIdentityError(
                    "LangGraph thread_id does not match the persisted workflow identity"
                )
            identity = identity_from_state(state)
            await self._dependencies.run_repository.upsert_status(
                identity,
                WorkflowStatus.PROCESSING,
                state["tenant_id"],
            )
            return {
                "status": WorkflowStatus.PROCESSING.value,
                "failure_message": None,
            }
        except Exception as exc:
            return await self._failure_update(state, "prepare_document", exc)

    async def retrieve_correction_context(self, state: GraphState) -> StateUpdate:
        try:
            extraction_payload = state["extraction"]
            if extraction_payload is None:
                raise ValueError("Baseline extraction is missing")
            baseline = self._dependencies.extraction_codec.load(
                extraction_payload,
                self._dependencies.output_schema,
            )
            context = ()
            reviewed_context = None
            reviewed_provider = self._dependencies.reviewed_example_context_provider
            if reviewed_provider is None:
                try:
                    context = await self._dependencies.correction_memory_repository.retrieve(
                        state["tenant_id"],
                        document_from_state(state),
                        self._dependencies.output_schema,
                        baseline.invoice,
                        self._dependencies.correction_context_limit,
                    )
                except Exception:
                    logger.warning(
                        "Correction-memory retrieval failed; continuing without old context"
                    )
            else:
                try:
                    reviewed_context = await reviewed_provider.retrieve_for_extraction(
                        state["tenant_id"],
                        baseline.invoice,
                        baseline.field_evidence,
                    )
                except Exception:
                    logger.warning(
                        "Reviewed-example retrieval failed; continuing without case context"
                    )
            return {
                "correction_context_retrieved": True,
                "correction_context": correction_events_to_state(context),
                "reviewed_example_context": reviewed_example_context_to_state(
                    reviewed_context
                ),
            }
        except Exception as exc:
            return await self._failure_update(state, "retrieve_correction_context", exc)

    async def extract_invoice(self, state: GraphState) -> StateUpdate:
        try:
            correction_context = correction_events_from_state(state["correction_context"])
            reviewed_context = reviewed_example_context_from_state(
                state["reviewed_example_context"]
            )
            focus_field_paths: tuple[str, ...] = ()
            if reviewed_context is not None and state["extraction"] is not None:
                baseline = self._dependencies.extraction_codec.load(
                    state["extraction"], self._dependencies.output_schema
                )
                review_paths = {
                    item.field_path
                    for item in self._dependencies.extraction_validator.validate(
                        baseline
                    ).field_decisions
                    if item.route is ValidationRoute.REVIEW_REQUIRED
                    and item.field_path is not None
                }
                case_paths = {
                    item.field_path
                    for item in (
                        *reviewed_context.verified_correct_examples,
                        *reviewed_context.reviewed_correction_examples,
                        *reviewed_context.reviewed_negative_examples,
                    )
                }
                focus_field_paths = tuple(sorted(review_paths & case_paths))
            result = await self._dependencies.extraction_service.extract(
                document=document_from_state(state),
                output_schema=self._dependencies.output_schema,
                tenant_id=state["tenant_id"],
                prompt_context=VisionPromptContext(
                    correction_events=correction_context,
                    reviewed_examples=reviewed_context,
                    focus_field_paths=focus_field_paths,
                ),
                trace_id=trace_id_from_state(state),
            )
            return {"extraction": self._dependencies.extraction_codec.dump(result)}
        except Exception as exc:
            return await self._failure_update(state, "extract_invoice", exc)

    async def validate_extraction(self, state: GraphState) -> StateUpdate:
        try:
            extraction_payload = state["extraction"]
            if extraction_payload is None:
                raise ValueError("Extraction result is missing")
            result = self._dependencies.extraction_codec.load(
                extraction_payload,
                self._dependencies.output_schema,
            )
            result = self._with_reviewed_example_conflicts(result, state)
            result = self._with_cross_page_field_conflicts(result)
            outcome = self._dependencies.extraction_validator.validate(result)
            outcome = self._with_field_binding_validation(result, outcome)
            await self._record_retrieval_route(state, outcome.requires_review)
            identity = identity_from_state(state)
            decisions_state = field_decisions_to_state(outcome.field_decisions)
            issues_state = issues_to_state(outcome.issues)
            extraction_state = self._dependencies.extraction_codec.dump(result)
            if outcome.rejected:
                failure_message = "validate_extraction rejected unusable document evidence"
                await self._dependencies.run_repository.upsert_status(
                    identity,
                    WorkflowStatus.FAILED,
                    state["tenant_id"],
                    failure_message=failure_message,
                    validation_route=ValidationRoute.REJECTED,
                )
                return {
                    "status": WorkflowStatus.FAILED.value,
                    "validation_route": ValidationRoute.REJECTED.value,
                    "validation_issues": issues_state,
                    "field_decisions": decisions_state,
                    "extraction": extraction_state,
                    "review_request": None,
                    "failure_message": failure_message,
                }
            if outcome.requires_review:
                request = self._review_request(
                    outcome.field_decisions,
                    result,
                )
                await self._dependencies.run_repository.upsert_status(
                    identity,
                    WorkflowStatus.PENDING_REVIEW,
                    state["tenant_id"],
                    validation_route=ValidationRoute.REVIEW_REQUIRED,
                )
                await self._dependencies.review_repository.upsert_pending_review(
                    identity,
                    request,
                )
                return {
                    "status": WorkflowStatus.PENDING_REVIEW.value,
                    "validation_route": ValidationRoute.REVIEW_REQUIRED.value,
                    "validation_issues": issues_state,
                    "field_decisions": decisions_state,
                    "extraction": extraction_state,
                    "review_request": review_request_to_state(request),
                    "human_correction": None,
                }
            await self._dependencies.run_repository.upsert_status(
                identity,
                WorkflowStatus.PROCESSING,
                state["tenant_id"],
                validation_route=ValidationRoute.ACCEPTED,
            )
            return {
                "status": WorkflowStatus.PROCESSING.value,
                "validation_route": ValidationRoute.ACCEPTED.value,
                "validation_issues": [],
                "field_decisions": decisions_state,
                "extraction": extraction_state,
                "review_request": None,
            }
        except Exception as exc:
            return await self._failure_update(state, "validate_extraction", exc)

    def request_human_review(self, state: GraphState) -> StateUpdate:
        """Interrupt without preceding side effects; this node restarts on resume."""

        resume_payload = interrupt(
            {
                "type": "invoice_human_review",
                "thread_id": state["thread_id"],
                "run_id": state["run_id"],
                "document_id": state["document_id"],
                "review_request": state["review_request"],
                "field_decisions": state["field_decisions"],
                "extraction": state["extraction"],
                "correction_context": state["correction_context"],
            }
        )
        try:
            correction = human_correction_from_state(resume_payload)
        except HumanCorrectionError as exc:
            request = self._append_review_reason(state, str(exc))
            return {
                "status": WorkflowStatus.PENDING_REVIEW.value,
                "review_request": review_request_to_state(request),
                "human_correction": None,
            }
        return {
            "status": WorkflowStatus.PROCESSING.value,
            "human_correction": human_correction_to_state(correction),
        }

    async def apply_human_correction(self, state: GraphState) -> StateUpdate:
        try:
            extraction_payload = state["extraction"]
            correction_payload = state["human_correction"]
            review_payload = state["review_request"]
            if extraction_payload is None or correction_payload is None or review_payload is None:
                raise HumanCorrectionError(
                    "Correction cannot be applied to incomplete workflow state"
                )
            result = self._dependencies.extraction_codec.load(
                extraction_payload,
                self._dependencies.output_schema,
            )
            correction = human_correction_from_state(correction_payload)
            review_request = review_request_from_state(review_payload)
            corrected_result, events = self._dependencies.correction_applier.apply(
                result,
                correction,
                review_request,
                self._dependencies.output_schema,
                document_from_state(state),
            )
            identity = identity_from_state(state)
            corrected_result = await self._apply_field_binding_reviews(
                state=state,
                identity=identity,
                result=corrected_result,
                correction=correction,
                review_request=review_request,
            )
            document = document_from_state(state)
            prior_events = correction_events_from_state(state["correction_events"])
            recorded = await (
                self._dependencies.memory_admission_service.record_review_facts(
                    identity=identity,
                    tenant_id=state["tenant_id"],
                    document=document,
                    original_result=result,
                    reviewed_result=corrected_result,
                    review=correction,
                    correction_events=events,
                )
            )
            await self._dependencies.review_repository.resolve_review(
                identity,
                state["tenant_id"],
                correction.reviewer_id,
                trace_id_from_state(state),
            )
            await self._dependencies.run_repository.upsert_status(
                identity,
                WorkflowStatus.PROCESSING,
                state["tenant_id"],
            )
            return {
                "status": WorkflowStatus.PROCESSING.value,
                "extraction": self._dependencies.extraction_codec.dump(corrected_result),
                "correction_events": correction_events_to_state(
                    (*prior_events, *(item.event for item in recorded.correction_events))
                ),
                "memory_recovery_id": recorded.recovery_id,
                "memory_trace_id": recorded.trace_id,
                "memory_status": recorded.status.value,
                "memory_error_code": None,
                "review_fact_persistence_failed": False,
                "human_correction": None,
                "review_request": None,
                "validation_route": None,
                "validation_issues": [],
                "field_decisions": [],
            }
        except HumanCorrectionError as exc:
            request = self._append_review_reason(state, str(exc))
            identity = identity_from_state(state)
            await self._dependencies.run_repository.upsert_status(
                identity,
                WorkflowStatus.PENDING_REVIEW,
                state["tenant_id"],
            )
            await self._dependencies.review_repository.upsert_pending_review(
                identity,
                request,
            )
            return {
                "status": WorkflowStatus.PENDING_REVIEW.value,
                "review_request": review_request_to_state(request),
                "human_correction": None,
                "review_fact_persistence_failed": True,
            }
        except WorkflowPersistenceError as exc:
            logger.warning(
                "human_review_facts_not_persisted",
                extra={
                    "tenant_id": state["tenant_id"],
                    "run_id": state["run_id"],
                    "error_type": type(exc).__name__,
                },
            )
            request = self._append_review_reason(
                state,
                "审核事实未能持久化，请重试本次审核提交。",
            )
            try:
                identity = identity_from_state(state)
                await self._dependencies.run_repository.upsert_status(
                    identity,
                    WorkflowStatus.PENDING_REVIEW,
                    state["tenant_id"],
                )
                await self._dependencies.review_repository.upsert_pending_review(
                    identity,
                    request,
                )
            except Exception:
                pass
            return {
                "status": WorkflowStatus.PENDING_REVIEW.value,
                "review_request": review_request_to_state(request),
                "human_correction": None,
            }
        except Exception as exc:
            return await self._failure_update(state, "apply_human_correction", exc)

    async def persist_result(self, state: GraphState) -> StateUpdate:
        try:
            if state["result_persisted"]:
                return {}
            extraction_payload = state["extraction"]
            if extraction_payload is None:
                raise ValueError("Final extraction result is missing")
            result = self._dependencies.extraction_codec.load(
                extraction_payload,
                self._dependencies.output_schema,
            )
            await self._dependencies.result_repository.upsert_result(
                identity_from_state(state),
                result,
            )
            return {"result_persisted": True}
        except Exception as exc:
            return await self._failure_update(state, "persist_result", exc)

    async def _apply_field_binding_reviews(
        self,
        *,
        state: GraphState,
        identity: WorkflowIdentity,
        result: ExtractionResult[InvoiceT],
        correction: HumanCorrection,
        review_request: ReviewRequest,
    ) -> ExtractionResult[InvoiceT]:
        required = {item.evidence_id for item in review_request.field_bindings}
        submitted = {item.evidence_id for item in correction.field_bindings}
        missing = required.difference(submitted)
        if missing:
            raise HumanCorrectionError(
                "Human review omitted required field bindings: "
                + ", ".join(sorted(missing))
            )
        unexpected = submitted.difference(required)
        if unexpected:
            raise HumanCorrectionError(
                "Human review contains unexpected field bindings: "
                + ", ".join(sorted(unexpected))
            )
        if not correction.field_bindings:
            return result
        service = self._dependencies.field_semantic_binding_service
        if service is None:
            raise HumanCorrectionError("Field semantic binding is not configured")
        current_by_id = {
            item.evidence_id: item for item in result.field_binding_evidence
        }
        requested_by_id = {
            item.evidence_id: item for item in review_request.field_bindings
        }
        reviewed_by_id: dict[str, FieldBindingEvidence] = {}
        try:
            for review in correction.field_bindings:
                current = current_by_id.get(review.evidence_id)
                requested = requested_by_id.get(review.evidence_id)
                if current is None or requested is None or current != requested:
                    raise HumanCorrectionError(
                        "Field binding review request is stale or outside this extraction"
                    )
                reviewed_by_id[review.evidence_id] = (
                    await service.confirm_reviewed_binding(
                        tenant_id=state["tenant_id"],
                        identity=identity,
                        reviewer_id=correction.reviewer_id,
                        invoice=result.invoice,
                        evidence=current,
                        review=review,
                    )
                )
        except HumanCorrectionError:
            raise
        except Exception as exc:
            logger.warning(
                "Unable to persist pending field alias from human review",
                extra={
                    "tenant_id": state["tenant_id"],
                    "run_id": identity.run_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise HumanCorrectionError(
                "Field binding review could not be persisted; retry the submission"
            ) from exc
        return replace(
            result,
            field_binding_evidence=tuple(
                reviewed_by_id.get(item.evidence_id, item)
                for item in result.field_binding_evidence
            ),
        )

    async def save_correction_memory(self, state: GraphState) -> StateUpdate:
        identity = identity_from_state(state)
        await self._dependencies.run_repository.upsert_status(
            identity,
            WorkflowStatus.COMPLETED,
            state["tenant_id"],
        )
        recovery_id = state.get("memory_recovery_id")
        if recovery_id is None:
            return {
                "correction_memory_saved": True,
                "memory_status": MemoryRecoveryStatus.COMPLETED.value,
                "memory_error_code": None,
                "status": WorkflowStatus.COMPLETED.value,
                "failure_message": None,
            }
        if state["correction_memory_saved"]:
            return {
                "status": WorkflowStatus.COMPLETED.value,
                "failure_message": None,
            }
        try:
            recovered = await (
                self._dependencies.memory_admission_service.materialize_review_candidates(
                    tenant_id=state["tenant_id"],
                    recovery_id=recovery_id,
                    worker_id=f"workflow:{identity.run_id}"[:128],
                    lease_seconds=self._dependencies.memory_recovery_lease_seconds,
                )
            )
            saved = recovered.status is MemoryRecoveryStatus.COMPLETED
            return {
                "correction_memory_saved": saved,
                "memory_admission_candidate_ids": list(recovered.example_ids),
                "memory_status": recovered.status.value,
                "memory_error_code": None,
                "status": WorkflowStatus.COMPLETED.value,
                "failure_message": None,
            }
        except Exception as exc:
            error_code = type(exc).__name__.strip().lower()[:128]
            memory_status = MemoryRecoveryStatus.PENDING.value
            memory_trace_id = state.get("memory_trace_id")
            try:
                recovery = await (
                    self._dependencies.memory_admission_service.get_review_recovery(
                        state["tenant_id"],
                        recovery_id,
                    )
                )
                if recovery is not None:
                    memory_status = recovery.status.value
                    memory_trace_id = recovery.trace_id
                    error_code = recovery.last_error_code or error_code
            except Exception:
                pass
            logger.warning(
                "correction_memory_deferred",
                extra={
                    "tenant_id": state["tenant_id"],
                    "run_id": identity.run_id,
                    "recovery_id": recovery_id,
                    "trace_id": memory_trace_id,
                    "error_code": error_code,
                },
            )
            return {
                "correction_memory_saved": False,
                "memory_status": memory_status,
                "memory_trace_id": memory_trace_id,
                "memory_error_code": error_code,
                "status": WorkflowStatus.COMPLETED.value,
                "failure_message": None,
            }

    @staticmethod
    def _with_cross_page_field_conflicts(
        result: ExtractionResult[InvoiceT],
    ) -> ExtractionResult[InvoiceT]:
        by_path: dict[str, dict[int, set[str]]] = {}
        for observation in result.ocr_observations:
            if observation.page_number is None or not observation.candidate_values:
                continue
            by_path.setdefault(observation.field_path, {}).setdefault(
                observation.page_number, set()
            ).update(value.strip() for value in observation.candidate_values if value.strip())
        additions = []
        existing = {(item.code, item.field_path) for item in result.anomalies}
        for field_path, pages in by_path.items():
            if len(pages) < 2 or len(set.union(*pages.values())) < 2:
                continue
            if ("cross_page_field_conflict", field_path) in existing:
                continue
            additions.append(ExtractionAnomaly(
                code="cross_page_field_conflict",
                message="同一字段在不同页出现不同候选值；请按证据页码和位置逐项核对，防止跨页串值。",
                field_path=field_path,
                page_number=None,
            ))
        return replace(result, anomalies=(*result.anomalies, *additions)) if additions else result

    def _with_field_binding_validation(
        self,
        result: ExtractionResult[InvoiceT],
        outcome: ValidationOutcome,
    ) -> ValidationOutcome:
        if self._dependencies.field_semantic_binding_service is None:
            return outcome
        pending = tuple(
            item
            for item in result.field_binding_evidence
            if item.binding_decision is None
            or item.binding_decision.status is not FieldBindingStatus.ACCEPTED
        )
        if not pending or outcome.rejected:
            return outcome
        additions = (
            ()
            if any(item.code == "field_binding_review_required" for item in outcome.issues)
            else (
                ValidationIssue(
                    code="field_binding_review_required",
                    message="图片字段标签未能确定性绑定到唯一 Schema 字段。",
                    field_path=None,
                ),
            )
        )
        return ValidationOutcome(
            route=ValidationRoute.REVIEW_REQUIRED,
            field_decisions=outcome.field_decisions,
            issues=(*outcome.issues, *additions),
        )

    async def _failure_update(
        self,
        state: GraphState,
        node_name: str,
        error: Exception,
    ) -> StateUpdate:
        message = f"{node_name} failed: {type(error).__name__}"
        error_code = None
        trace_id = None
        if isinstance(error, VisionExtractionError):
            error_code = error.reason_code
            trace_id = error.trace_id
            message += f" [{error_code}]"
            if trace_id is not None:
                message += f" trace_id={trace_id}"
        logger.error(
            "workflow_node_failed",
            extra={
                "run_id": state["run_id"],
                "document_id": state["document_id"],
                "error_type": type(error).__name__,
                "error_code": error_code,
                "trace_id": trace_id,
            },
        )
        try:
            await self._dependencies.run_repository.upsert_status(
                identity_from_state(state),
                WorkflowStatus.FAILED,
                state["tenant_id"],
                failure_message=message,
            )
        except Exception:
            pass
        return {
            "status": WorkflowStatus.FAILED.value,
            "failure_message": message,
        }

    async def _record_retrieval_route(
        self,
        state: GraphState,
        review_required: bool,
    ) -> None:
        repository = self._dependencies.retrieval_telemetry_repository
        context = reviewed_example_context_from_state(state["reviewed_example_context"])
        if repository is None or context is None:
            return
        try:
            await repository.mark_review_required(
                state["tenant_id"],
                context.trace_ids,
                review_required,
            )
        except Exception:
            logger.warning("Unable to bind retrieval trace to validation route")

    @staticmethod
    def _with_reviewed_example_conflicts(
        result: ExtractionResult[InvoiceT],
        state: GraphState,
    ) -> ExtractionResult[InvoiceT]:
        context = reviewed_example_context_from_state(
            state["reviewed_example_context"]
        )
        if context is None or not context.conflicting_field_paths:
            return result
        existing = {
            (anomaly.code, anomaly.field_path)
            for anomaly in result.anomalies
        }
        additions = tuple(
            ExtractionAnomaly(
                code="reviewed_examples_conflict",
                message="同一历史错误模式存在互相冲突的人工审核结果，必须人工核对当前图片。",
                field_path=field_path,
                page_number=None,
            )
            for field_path in context.conflicting_field_paths
            if ("reviewed_examples_conflict", field_path) not in existing
        )
        return replace(result, anomalies=(*result.anomalies, *additions))

    @classmethod
    def _review_request(
        cls,
        decisions: tuple[FieldDecision, ...],
        result: ExtractionResult[InvoiceT],
    ) -> ReviewRequest:
        review_paths = frozenset(
            decision.field_path
            for decision in decisions
            if decision.route is ValidationRoute.REVIEW_REQUIRED
            and decision.field_path is not None
        )
        return ReviewRequest(
            fields=tuple(
                ReviewField(
                    field_path=decision.field_path,
                    current_value=decision.current_value,
                    candidate_values=decision.candidate_values,
                    triggered_rules=tuple(
                        signal.rule
                        for signal in decision.signals
                        if signal.verdict is not SignalVerdict.PASSED
                    ),
                    reasons=tuple(
                        signal.message
                        for signal in decision.signals
                        if signal.verdict is not SignalVerdict.PASSED
                    ),
                    user_action=decision.user_action
                    or "请核对原始文档并提交明确值。",
                )
                for decision in decisions
                if decision.route is ValidationRoute.REVIEW_REQUIRED
            ),
            field_bindings=tuple(
                item
                for item in result.field_binding_evidence
                if item.binding_decision is None
                or item.binding_decision.status is not FieldBindingStatus.ACCEPTED
            ),
            evidence_sources=cls._review_evidence_sources(result, review_paths),
        )

    @classmethod
    def _review_evidence_sources(
        cls,
        result: ExtractionResult[InvoiceT],
        review_paths: frozenset[str],
    ) -> tuple[ReviewEvidenceSummary, ...]:
        comparisons = {
            item.canonical_field_path: item for item in result.ocr_comparisons
        }
        accepted_bindings: dict[str, FieldBindingEvidence] = {}
        for binding in sorted(
            result.field_binding_evidence,
            key=lambda item: item.evidence_id,
        ):
            decision = binding.binding_decision
            if (
                decision is not None
                and decision.status is FieldBindingStatus.ACCEPTED
                and decision.selected_canonical_field_path is not None
            ):
                accepted_bindings.setdefault(
                    decision.selected_canonical_field_path,
                    binding,
                )

        summaries: list[ReviewEvidenceSummary] = []
        for evidence in result.field_evidence:
            field_path = evidence.field_path
            if field_path not in review_paths:
                continue
            comparison = comparisons.get(field_path)
            binding = accepted_bindings.get(field_path)
            summaries.append(
                ReviewEvidenceSummary(
                    field_path=field_path,
                    source_type=evidence.source.value,
                    source_id=(
                        f"vision:{field_path}:page:{evidence.page_number or 'unknown'}"
                    ),
                    candidate_values=cls._review_candidates(
                        evidence.candidate_values
                    ),
                    page_number=evidence.page_number,
                    bounding_box=(
                        tuple(float(item) for item in binding.bounding_box)
                        if binding is not None and binding.bounding_box is not None
                        else None
                    ),
                    provider_name=None,
                    provider_version=None,
                    model_version=None,
                    provider_score=None,
                    source_reference=(
                        binding.image_reference if binding is not None else None
                    ),
                    comparison_outcome=(
                        comparison.outcome.value if comparison is not None else None
                    ),
                    reason_codes=(
                        comparison.reason_codes if comparison is not None else ()
                    ),
                )
            )

        for observation in result.ocr_observations:
            field_path = observation.field_path
            if field_path not in review_paths:
                continue
            comparison = comparisons.get(field_path)
            summaries.append(
                ReviewEvidenceSummary(
                    field_path=field_path,
                    source_type="ocr",
                    source_id=observation.source_id,
                    candidate_values=cls._review_candidates(
                        observation.candidate_values
                    ),
                    page_number=observation.page_number,
                    bounding_box=observation.bounding_box,
                    provider_name=observation.provider_name,
                    provider_version=observation.provider_version,
                    model_version=observation.model_version,
                    provider_score=observation.provider_score,
                    source_reference=observation.source_reference,
                    comparison_outcome=(
                        comparison.outcome.value if comparison is not None else None
                    ),
                    reason_codes=tuple(
                        sorted(
                            {
                                *observation.anomalies,
                                *observation.binding_reason_codes,
                                *(
                                    comparison.reason_codes
                                    if comparison is not None
                                    else ()
                                ),
                            }
                        )
                    ),
                )
            )
        unique = {
            (
                item.field_path,
                item.source_type,
                item.source_id,
                item.candidate_values,
                item.page_number,
                item.bounding_box,
                item.provider_name,
                item.provider_version,
                item.model_version,
                item.provider_score,
                item.source_reference,
                item.comparison_outcome,
                item.reason_codes,
            ): item
            for item in summaries
        }
        return tuple(unique[key] for key in sorted(unique, key=repr))

    @staticmethod
    def _review_candidates(values: tuple[str, ...]) -> tuple[str, ...]:
        """Return a stable, trimmed review projection rather than raw line text."""

        return tuple(sorted({value.strip() for value in values if value.strip()}))

    @staticmethod
    def _append_review_reason(state: GraphState, reason: str) -> ReviewRequest:
        payload = state["review_request"]
        request = review_request_from_state(payload) if payload is not None else ReviewRequest(())
        return ReviewRequest(
            fields=(
                *request.fields,
                ReviewField(
                    field_path=None,
                    current_value=None,
                    candidate_values=(),
                    triggered_rules=("human_correction_payload",),
                    reasons=(reason,),
                    user_action="请修正提交格式并重新提交完整人工修正。",
                ),
            ),
            field_bindings=request.field_bindings,
            evidence_sources=request.evidence_sources,
        )
