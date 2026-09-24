"""Translate application read models into stable API schemas."""

from collections.abc import Mapping

from invoice_intelligence.api.schemas.memory import (
    EvaluationBindingsResponse,
    EvaluationRunResponse,
    EvaluationVariantResultResponse,
    ExampleEvidenceReferenceResponse,
    FieldAliasCandidateResponse,
    FieldAliasDecisionResponse,
    FieldAliasResponse,
    FieldAliasSupportResponse,
    FieldContextAnchorResponse,
    FieldSemanticConflictListResponse,
    FieldSemanticDefinitionResponse,
    FieldSemanticIndexResponse,
    FieldSemanticListResponse,
    FieldSemanticProjectionExecutionResponse,
    GovernanceAuditListResponse,
    GovernanceAuditResponse,
    GovernanceOperationResponse,
    IndexProjectionCountsResponse,
    IndexProjectionExecutionResponse,
    MemoryAdmissionBatchDecisionItemResponse,
    MemoryAdmissionBatchDecisionResponse,
    MemoryAdmissionDecisionResponse,
    MemoryAdmissionDecisionResultResponse,
    MemoryAdmissionDetailResponse,
    MemoryAdmissionListResponse,
    MemoryAdmissionResponse,
    MemoryConflictReevaluationTargetResponse,
    MemoryConflictResolutionDecisionResponse,
    MemoryConflictResolutionResultResponse,
    MemoryConflictResponse,
    MemoryExampleListResponse,
    MemoryExampleProjectionListResponse,
    MemoryExampleProjectionResponse,
    MemoryExampleResponse,
    MemoryFeedbackResponse,
    MemoryIndexResponse,
    MemoryQualityAssessmentResponse,
    MemoryQualitySignalResponse,
    OCRMetricsSummaryResponse,
    OCRProviderVersionSummaryResponse,
    PromotionCandidateResponse,
    RetrievalMetricSummaryResponse,
)
from invoice_intelligence.api.schemas.workflows import (
    ExtractionResultEnvelopeResponse,
    ExtractionResultResponse,
    ExtractionRunResponse,
    ReviewRequestResponse,
    ReviewTaskResponse,
)
from invoice_intelligence.application.ports.business_persistence import (
    ExtractionResultRecord,
    ExtractionRunRecord,
    ReviewTaskRecord,
)
from invoice_intelligence.application.ports.observability import OCRMetricsSummary
from invoice_intelligence.application.services.memory_governance import (
    FieldAliasDecisionResult,
    FieldAliasGovernanceItem,
    FieldSemanticConflictPage,
    FieldSemanticGovernancePage,
    FieldSemanticProjectionExecution,
    GovernanceAuditPage,
    GovernanceOperationResult,
    IndexProjectionExecution,
    MemoryAdmissionBatchDecisionResult,
    MemoryAdmissionDecisionResult,
    MemoryAdmissionDetail,
    MemoryAdmissionGovernanceItem,
    MemoryAdmissionPage,
    MemoryConflictResolutionResult,
    MemoryExamplePage,
    MemoryExampleProjectionPage,
    MemoryFeedbackResult,
    MemoryIndexView,
)
from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecision,
    MemoryConflictRecord,
)
from invoice_intelligence.domain.evaluation import EvaluationRun
from invoice_intelligence.domain.examples import ExampleEvidenceReference, ReviewedExample
from invoice_intelligence.domain.field_semantics import (
    FieldAlias,
    FieldAliasStatus,
    FieldContextAnchor,
    FieldSemanticDefinition,
    FieldSemanticIndexVersionRecord,
)


def present_run(record: ExtractionRunRecord) -> ExtractionRunResponse:
    return ExtractionRunResponse(
        run_id=record.identity.run_id,
        document_id=record.identity.document_id,
        status=record.status,
        validation_route=record.validation_route,
        failure_message=record.failure_message,
        memory_status=(record.memory_status.value if record.memory_status else None),
        memory_trace_id=record.memory_trace_id,
        memory_error_code=record.memory_error_code,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def present_result(
    record: ExtractionResultRecord,
) -> ExtractionResultEnvelopeResponse:
    return ExtractionResultEnvelopeResponse(
        run_id=record.run_id,
        document_id=record.document_id,
        result=ExtractionResultResponse.model_validate(record.payload),
        created_at=record.created_at,
    )


def present_review(
    record: ReviewTaskRecord,
    extraction_payload: Mapping[str, object],
) -> ReviewTaskResponse:
    extraction = ExtractionResultResponse.model_validate(extraction_payload)
    return ReviewTaskResponse(
        run_id=record.run_id,
        status=record.status,
        request=ReviewRequestResponse.model_validate(record.request_payload),
        current_invoice=extraction.invoice,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        resolved_at=record.resolved_at,
    )


def present_memory_example(example: ReviewedExample) -> MemoryExampleResponse:
    evidence = example.evidence_reference
    return MemoryExampleResponse(
        example_id=example.example_id,
        source_feedback_id=example.source_feedback_id,
        source_event_id=example.source_event_id,
        document_id=example.document_id,
        run_id=example.run_id,
        document_type=example.document_type,
        field_path=example.field_path,
        schema_version=example.schema_version,
        catalog_version=example.catalog_version,
        model_version=example.model_version.value,
        prompt_version=example.prompt_version.value,
        label_type=example.label_type,
        model_value=example.model_value,
        reviewed_value=example.reviewed_value,
        correction_reason=example.correction_reason,
        vendor_fingerprint=example.vendor_fingerprint,
        template_fingerprint=example.template_fingerprint,
        evidence_reference=_present_example_evidence(evidence),
        reviewer_id=example.reviewer_id,
        is_reviewed=example.is_reviewed,
        is_valid=example.is_valid,
        invalidated_reason=example.invalidated_reason,
        invalidated_at=example.invalidated_at,
        occurrence_count=example.occurrence_count,
        created_at=example.created_at,
        last_seen_at=example.last_seen_at,
    )


def present_memory_example_page(page: MemoryExamplePage) -> MemoryExampleListResponse:
    return MemoryExampleListResponse(
        items=tuple(present_memory_example(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


def present_memory_example_projections(
    page: MemoryExampleProjectionPage,
) -> MemoryExampleProjectionListResponse:
    return MemoryExampleProjectionListResponse(
        example_id=page.example_id,
        admission_status=page.admission_status,
        eligible_for_long_term_retrieval=page.eligible_for_long_term_retrieval,
        state_source="postgresql",
        milvus_realtime_verified=False,
        items=tuple(
            MemoryExampleProjectionResponse(
                index_version=item.index_version.value,
                projection_status=item.status,
                attempt_count=item.attempt_count,
                projection_checksum=item.projection_checksum,
                last_error_code=item.last_error_code,
                processing_started_at=item.processing_started_at,
                lease_expires_at=None,
                created_at=item.created_at,
                updated_at=item.updated_at,
                indexed_at=item.indexed_at,
                invalidated_at=item.invalidated_at,
            )
            for item in page.items
        ),
        next_cursor=page.next_cursor,
    )


def present_memory_admission(
    item: MemoryAdmissionGovernanceItem,
) -> MemoryAdmissionResponse:
    admission = item.admission
    example = item.example
    return MemoryAdmissionResponse(
        admission_id=admission.example_id,
        example_id=admission.example_id,
        document_id=example.document_id,
        run_id=example.run_id,
        document_type=example.document_type,
        field_path=example.field_path,
        schema_version=example.schema_version,
        catalog_version=example.catalog_version,
        model_version=example.model_version.value,
        prompt_version=example.prompt_version.value,
        label_type=example.label_type,
        model_value=example.model_value,
        reviewed_value=example.reviewed_value,
        correction_reason=example.correction_reason,
        evidence_reference=_present_example_evidence(example.evidence_reference),
        source_reviewer_id=example.reviewer_id,
        is_reviewed=example.is_reviewed,
        is_valid=example.is_valid,
        status=admission.status,
        current_decision_id=admission.current_decision_id,
        policy_version=admission.policy_version,
        revision=admission.revision,
        created_at=admission.created_at,
        updated_at=admission.updated_at,
    )


def present_memory_admission_page(
    page: MemoryAdmissionPage,
) -> MemoryAdmissionListResponse:
    return MemoryAdmissionListResponse(
        items=tuple(present_memory_admission(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


def present_memory_conflict(conflict: MemoryConflictRecord) -> MemoryConflictResponse:
    return MemoryConflictResponse(
        conflict_id=conflict.conflict_id,
        example_ids=conflict.example_ids,
        field_alias_candidate_ids=conflict.field_alias_candidate_ids,
        document_type=conflict.document_type,
        field_path=conflict.field_path,
        schema_version=conflict.schema_version,
        conflict_type=conflict.conflict_type,
        reason_codes=conflict.reason_codes,
        evidence_references=conflict.evidence_references,
        candidate_field_paths=conflict.candidate_field_paths,
        status=conflict.status,
        detected_at=conflict.detected_at,
        resolved_at=conflict.resolved_at,
        resolution_decision_id=conflict.resolution_decision_id,
    )


def present_memory_conflict_resolution(
    result: MemoryConflictResolutionResult,
) -> MemoryConflictResolutionResultResponse:
    decision = result.decision
    audit = result.audit_event
    return MemoryConflictResolutionResultResponse(
        conflict=present_memory_conflict(result.conflict),
        decision=MemoryConflictResolutionDecisionResponse(
            resolution_decision_id=decision.resolution_decision_id,
            conflict_id=decision.conflict_id,
            previous_status=decision.previous_status,
            target_status=decision.target_status,
            selected_canonical_field_path=(
                decision.selected_canonical_field_path
            ),
            reviewer_id=decision.reviewer_id,
            reason=decision.reason,
            resolution_note=decision.resolution_note,
            policy_version=decision.policy_version,
            decided_at=decision.decided_at,
            reevaluation_targets=tuple(
                MemoryConflictReevaluationTargetResponse(
                    target_type=target.target_type.value,
                    target_id=target.target_id,
                    status="pending",
                )
                for target in decision.reevaluation_targets
            ),
        ),
        reevaluation_registered=result.reevaluation_registered,
        audit_id=audit.audit_id if audit is not None else None,
        resource_version=audit.resource_version if audit is not None else None,
        trace_id=audit.trace_id if audit is not None else None,
    )


def present_memory_admission_detail(
    detail: MemoryAdmissionDetail,
) -> MemoryAdmissionDetailResponse:
    base = present_memory_admission(detail.item).model_dump()
    return MemoryAdmissionDetailResponse(
        **base,
        assessments=tuple(
            MemoryQualityAssessmentResponse(
                assessment_id=assessment.assessment_id,
                source=assessment.source,
                signals=tuple(
                    MemoryQualitySignalResponse(
                        code=signal.code,
                        source=signal.source,
                        verdict=signal.verdict.value,
                        score=signal.score,
                        message=signal.message,
                        field_path=signal.field_path,
                        evidence_references=signal.evidence_references,
                    )
                    for signal in assessment.signals
                ),
                quality_score=assessment.quality_score,
                recommendation=assessment.recommendation,
                reason_codes=assessment.reason_codes,
                policy_version=assessment.policy_version,
                model_version=(
                    assessment.model_version.value
                    if assessment.model_version is not None
                    else None
                ),
                prompt_version=(
                    assessment.prompt_version.value
                    if assessment.prompt_version is not None
                    else None
                ),
                advisory_only=assessment.advisory_only,
                assessed_at=assessment.created_at,
            )
            for assessment in detail.assessments
        ),
        decisions=tuple(
            _present_admission_decision(decision) for decision in detail.decisions
        ),
        open_conflicts=tuple(
            present_memory_conflict(conflict) for conflict in detail.conflicts
        ),
    )


def present_memory_admission_decision_result(
    result: MemoryAdmissionDecisionResult,
) -> MemoryAdmissionDecisionResultResponse:
    audit = result.audit_event
    return MemoryAdmissionDecisionResultResponse(
        admission=present_memory_admission(result.item),
        decision=_present_admission_decision(result.decision),
        projection_reconciled=result.projection_reconciled,
        audit_id=audit.audit_id if audit is not None else None,
        resource_version=audit.resource_version if audit is not None else None,
        trace_id=audit.trace_id if audit is not None else None,
    )


def present_memory_admission_batch_decision(
    result: MemoryAdmissionBatchDecisionResult,
) -> MemoryAdmissionBatchDecisionResponse:
    items = tuple(
        MemoryAdmissionBatchDecisionItemResponse(
            admission_id=item.admission_id,
            succeeded=item.result is not None,
            result=(
                present_memory_admission_decision_result(item.result)
                if item.result is not None
                else None
            ),
            error_type=item.error_type,
            error_message=item.error_message,
        )
        for item in result.items
    )
    succeeded_count = sum(item.succeeded for item in items)
    return MemoryAdmissionBatchDecisionResponse(
        target_status=result.target_status,
        succeeded_count=succeeded_count,
        failed_count=len(items) - succeeded_count,
        items=items,
    )


def _present_example_evidence(
    evidence: ExampleEvidenceReference,
) -> ExampleEvidenceReferenceResponse:
    return ExampleEvidenceReferenceResponse(
        document_reference=evidence.document_reference,
        image_reference=evidence.image_reference,
        page_number=evidence.page_number,
        evidence_source=evidence.evidence_source,
        candidate_values=evidence.candidate_values,
        readability=evidence.readability,
        validation_signals=evidence.validation_signals,
        ambiguous=evidence.ambiguous,
    )


def _present_admission_decision(
    decision: MemoryAdmissionDecision,
) -> MemoryAdmissionDecisionResponse:
    return MemoryAdmissionDecisionResponse(
        decision_id=decision.decision_id,
        previous_status=decision.previous_status,
        status=decision.status,
        authority=decision.authority,
        decided_by=decision.decided_by,
        reason=decision.reason,
        reason_codes=decision.reason_codes,
        assessment_ids=decision.assessment_ids,
        conflict_ids=decision.conflict_ids,
        policy_version=decision.policy_version,
        revision=decision.revision,
        decided_at=decision.decided_at,
    )


def present_field_semantics(
    page: FieldSemanticGovernancePage,
) -> FieldSemanticListResponse:
    return FieldSemanticListResponse(
        definitions=tuple(
            _present_field_semantic_definition(item) for item in page.definitions
        ),
        alias_candidates=tuple(
            _present_field_alias_candidate(item) for item in page.alias_candidates
        ),
        next_cursor=page.next_cursor,
    )


def present_field_semantic_conflicts(
    page: FieldSemanticConflictPage,
) -> FieldSemanticConflictListResponse:
    return FieldSemanticConflictListResponse(
        items=tuple(present_memory_conflict(item) for item in page.items),
        next_cursor=page.next_cursor,
    )


def present_field_alias_decision(
    result: FieldAliasDecisionResult,
) -> FieldAliasDecisionResponse:
    promoted = result.promoted_catalog_version
    audit = result.audit_event
    return FieldAliasDecisionResponse(
        alias=_present_field_alias_candidate(result.item),
        promoted_catalog_version=(promoted.value if promoted is not None else None),
        catalog_activation_required=(
            result.item.candidate.status is FieldAliasStatus.APPROVED
            and promoted is not None
        ),
        audit_id=audit.audit_id if audit is not None else None,
        resource_version=audit.resource_version if audit is not None else None,
        trace_id=audit.trace_id if audit is not None else None,
    )


def _present_field_alias_candidate(
    item: FieldAliasGovernanceItem,
) -> FieldAliasCandidateResponse:
    candidate = item.candidate
    support = item.support
    return FieldAliasCandidateResponse(
        alias_id=candidate.candidate_id,
        schema_version=candidate.schema_version,
        document_type=candidate.document_type,
        canonical_field_path=candidate.canonical_field_path,
        alias_text=candidate.alias_text,
        normalized_alias=candidate.normalized_alias,
        policy_version=candidate.policy_version,
        revision=candidate.revision,
        status=candidate.status,
        canonical_collision=candidate.canonical_collision,
        support=FieldAliasSupportResponse(
            window_started_at=support.window_started_at,
            window_ended_at=support.window_ended_at,
            source_count=support.source_count,
            distinct_documents=support.distinct_documents,
            distinct_templates=support.distinct_templates,
            distinct_reviewers=support.distinct_reviewers,
        ),
        source_reviewer_count=item.source_reviewer_count,
        conflict_ids=item.conflict_ids,
        submitted_at=candidate.submitted_at,
        updated_at=candidate.updated_at,
        reviewed_by=candidate.reviewed_by,
        reviewed_at=candidate.reviewed_at,
        review_reason=candidate.review_reason,
        promoted_catalog_version=(
            candidate.promoted_catalog_version.value
            if candidate.promoted_catalog_version is not None
            else None
        ),
    )


def _present_field_semantic_definition(
    definition: FieldSemanticDefinition,
) -> FieldSemanticDefinitionResponse:
    return FieldSemanticDefinitionResponse(
        schema_version=definition.schema_version,
        document_type=definition.document_type,
        canonical_field_path=definition.canonical_field_path,
        display_name=definition.display_name,
        description=definition.description,
        value_type=definition.value_type,
        aliases=tuple(_present_field_alias(item) for item in definition.aliases),
        negative_aliases=tuple(
            _present_field_alias(item) for item in definition.negative_aliases
        ),
        context_anchors=tuple(
            _present_field_context_anchor(item) for item in definition.context_anchors
        ),
        catalog_version=definition.catalog_version.value,
        is_valid=definition.is_valid,
    )


def _present_field_alias(alias: FieldAlias) -> FieldAliasResponse:
    return FieldAliasResponse(
        alias_id=alias.alias_id,
        alias_text=alias.alias_text,
        normalized_alias=alias.normalized_alias,
        is_negative=alias.is_negative,
        status=alias.status,
        is_valid=alias.is_valid,
        context_anchors=tuple(
            _present_field_context_anchor(item) for item in alias.context_anchors
        ),
        reviewed_by=alias.reviewed_by,
        reviewed_at=alias.reviewed_at,
        review_reason=alias.review_reason,
    )


def _present_field_context_anchor(
    anchor: FieldContextAnchor,
) -> FieldContextAnchorResponse:
    return FieldContextAnchorResponse(
        anchor_id=anchor.anchor_id,
        text=anchor.text,
        normalized_text=anchor.normalized_text,
        relation=anchor.relation,
        max_distance=anchor.max_distance,
        is_negative=anchor.is_negative,
    )


def present_governance_operation(
    result: GovernanceOperationResult,
) -> GovernanceOperationResponse:
    return GovernanceOperationResponse(
        action=result.action,
        resource_id=result.resource_id,
        changed_count=result.changed_count,
        audit_id=result.audit_id,
        resource_version=result.resource_version,
        trace_id=result.trace_id,
        performed_at=result.performed_at,
    )


def present_governance_audits(page: GovernanceAuditPage) -> GovernanceAuditListResponse:
    return GovernanceAuditListResponse(
        items=tuple(
            GovernanceAuditResponse(
                audit_id=item.audit_id,
                operation=item.action,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                actor=item.reviewer_id,
                reason=item.reason,
                resource_version=item.resource_version,
                trace_id=item.trace_id,
                timestamp=item.created_at,
            )
            for item in page.items
        ),
        next_cursor=page.next_cursor,
    )


def present_memory_index(view: MemoryIndexView) -> MemoryIndexResponse:
    index = view.index
    counts = index.projection_counts
    metrics = view.metrics
    audit = view.audit_event
    return MemoryIndexResponse(
        index_version=index.index_version.value,
        schema_version=index.schema_version,
        dense_model_version=index.dense_model_version.value,
        sparse_model_version=(
            index.sparse_model_version.value if index.sparse_model_version else None
        ),
        rerank_model_version=(
            index.rerank_model_version.value if index.rerank_model_version else None
        ),
        prompt_version=index.prompt_version.value,
        is_active=index.is_active,
        is_valid=index.is_valid,
        invalidated_reason=index.invalidated_reason,
        projection_counts=IndexProjectionCountsResponse(
            pending=counts.pending,
            processing=counts.processing,
            indexed=counts.indexed,
            failed=counts.failed,
            invalidated=counts.invalidated,
            total=counts.total,
        ),
        metrics=RetrievalMetricSummaryResponse(
            trace_count=metrics.trace_count,
            empty_retrieval_rate=metrics.empty_retrieval_rate,
            positive_hit_rate=metrics.positive_hit_rate,
            negative_hit_rate=metrics.negative_hit_rate,
            review_required_rate=metrics.review_required_rate,
            remote_model_error_rate=metrics.remote_model_error_rate,
            total_input_tokens=metrics.total_input_tokens,
            total_output_tokens=metrics.total_output_tokens,
            total_estimated_cost=metrics.total_estimated_cost,
        ),
        created_at=index.created_at,
        activated_at=index.activated_at,
        retired_at=index.retired_at,
        invalidated_at=index.invalidated_at,
        audit_id=audit.audit_id if audit is not None else None,
        resource_version=audit.resource_version if audit is not None else None,
        trace_id=audit.trace_id if audit is not None else None,
    )


def present_ocr_metrics(metrics: OCRMetricsSummary) -> OCRMetricsSummaryResponse:
    return OCRMetricsSummaryResponse(
        provider_call_count=metrics.provider_call_count,
        page_call_count=metrics.page_call_count,
        total_latency_ms=metrics.total_latency_ms,
        average_call_latency_ms=metrics.average_call_latency_ms,
        average_page_latency_ms=metrics.average_page_latency_ms,
        success_count=metrics.success_count,
        timeout_count=metrics.timeout_count,
        circuit_open_count=metrics.circuit_open_count,
        schema_error_count=metrics.schema_error_count,
        other_error_count=metrics.other_error_count,
        text_box_count=metrics.text_box_count,
        bound_field_count=metrics.bound_field_count,
        corroborated_count=metrics.corroborated_count,
        conflicting_count=metrics.conflicting_count,
        ocr_only_count=metrics.ocr_only_count,
        vision_only_count=metrics.vision_only_count,
        unresolved_count=metrics.unresolved_count,
        unavailable_count=metrics.unavailable_count,
        empty_ocr_rate=metrics.empty_ocr_rate,
        conflict_review_required_rate=metrics.conflict_review_required_rate,
        providers=tuple(
            OCRProviderVersionSummaryResponse(
                provider_name=item.provider_name,
                provider_version=item.provider_version,
                model_version=item.model_version,
                config_version=item.config_version,
                call_count=item.call_count,
            )
            for item in metrics.providers
        ),
    )


def present_index_projection_execution(
    result: IndexProjectionExecution,
) -> IndexProjectionExecutionResponse:
    return IndexProjectionExecutionResponse(
        claimed=result.batch.claimed,
        indexed=result.batch.indexed,
        failed=result.batch.failed,
        index=present_memory_index(result.index),
    )


def present_field_semantic_index(
    index: FieldSemanticIndexVersionRecord,
) -> FieldSemanticIndexResponse:
    return FieldSemanticIndexResponse(
        index_version=index.index_version.value,
        schema_version=index.schema_version,
        catalog_version=index.catalog_version.value,
        dense_model_version=index.dense_model_version.value,
        sparse_model_version=(
            index.sparse_model_version.value if index.sparse_model_version else None
        ),
        is_active=index.is_active,
        is_valid=index.is_valid,
        pending_count=index.pending_count,
        processing_count=index.processing_count,
        indexed_count=index.indexed_count,
        failed_count=index.failed_count,
        invalidated_count=index.invalidated_count,
        created_at=index.created_at,
        activated_at=index.activated_at,
        retired_at=index.retired_at,
        invalidated_at=index.invalidated_at,
        invalidated_reason=index.invalidated_reason,
    )


def present_field_semantic_projection_execution(
    result: FieldSemanticProjectionExecution,
) -> FieldSemanticProjectionExecutionResponse:
    return FieldSemanticProjectionExecutionResponse(
        claimed=result.batch.claimed,
        indexed=result.batch.indexed,
        failed=result.batch.failed,
        index=present_field_semantic_index(result.index),
    )


def present_memory_feedback(result: MemoryFeedbackResult) -> MemoryFeedbackResponse:
    feedback = result.feedback
    audit = result.audit_event
    return MemoryFeedbackResponse(
        feedback_id=feedback.feedback_id,
        trace_id=feedback.trace_id,
        example_id=feedback.example_id,
        label=feedback.label,
        reviewer_id=feedback.reviewer_id,
        reason=feedback.reason,
        created_at=feedback.created_at,
        audit_id=audit.audit_id if audit is not None else None,
        governance_trace_id=audit.trace_id if audit is not None else None,
    )


def present_evaluation_run(run: EvaluationRun) -> EvaluationRunResponse:
    bindings = run.bindings
    return EvaluationRunResponse(
        evaluation_run_id=run.evaluation_run_id,
        dataset_id=run.dataset_id,
        dataset_version=run.dataset_version,
        schema_version=run.schema_version,
        suite=run.suite,
        bindings=EvaluationBindingsResponse(
            index_version=bindings.index_version.value,
            model_version=bindings.model_version.value,
            prompt_version=bindings.prompt_version.value,
            retrieval_policy_version=bindings.retrieval_policy_version.value,
            threshold_version=bindings.threshold_version,
            catalog_version=bindings.catalog_version,
            admission_policy_version=bindings.admission_policy_version,
            field_binding_policy_version=bindings.field_binding_policy_version,
        ),
        variants=run.variants,
        status=run.status,
        results=tuple(
            EvaluationVariantResultResponse.model_validate(item)
            for item in run.results
        ),
        promotion_candidates=tuple(
            PromotionCandidateResponse(
                candidate_id=item.candidate_id,
                evaluation_run_id=item.evaluation_run_id,
                baseline_variant=item.baseline_variant,
                candidate_variant=item.candidate_variant,
                metric_deltas=item.metric_deltas,
                rationale_codes=item.rationale_codes,
                created_at=item.created_at,
                status=item.status,
                requires_human_approval=item.requires_human_approval,
                may_modify_production=item.may_modify_production,
            )
            for item in run.promotion_candidates
        ),
        leakage_check_passed=run.leakage_check_passed,
        report_schema_version=run.report_schema_version,
        artifact_references=run.artifact_references,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        failure_code=run.failure_code,
    )
