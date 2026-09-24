"""Primitive, checkpoint-safe state for the deterministic invoice workflow."""

import math
from datetime import datetime
from typing import NotRequired, TypedDict, cast

from invoice_intelligence.application.errors import HumanCorrectionError
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.examples import (
    ExampleLabelType,
    IndexVersion,
    RetrievalPolicyVersion,
    ReviewedExamplePromptContext,
    ReviewedExamplePromptReference,
)
from invoice_intelligence.domain.field_semantics import (
    FieldBindingDecisionAuthority,
    FieldBindingDecisionSummary,
    FieldBindingEvidence,
    FieldBindingReviewDecision,
    FieldBindingStatus,
    FieldContextObservation,
    FieldContextRelation,
    FieldSemanticCatalogVersion,
)
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    FieldDecision,
    FieldReviewDecision,
    HumanCorrection,
    HumanReviewAction,
    JsonValue,
    ReviewEvidenceSummary,
    ReviewField,
    ReviewRequest,
    ValidationIssue,
    WorkflowIdentity,
    WorkflowStatus,
)


class GraphState(TypedDict):
    """Persist primitive workflow data and document references, never file bytes."""

    thread_id: str
    run_id: str
    document_id: str
    tenant_id: str
    trace_id: NotRequired[str]
    storage_uri: str
    mime_type: str
    checksum: str
    status: str
    correction_context_retrieved: bool
    correction_context: list[dict[str, JsonValue]]
    reviewed_example_context: dict[str, JsonValue] | None
    extraction: dict[str, JsonValue] | None
    validation_route: str | None
    validation_issues: list[dict[str, JsonValue]]
    field_decisions: list[dict[str, JsonValue]]
    review_request: dict[str, JsonValue] | None
    human_correction: dict[str, JsonValue] | None
    correction_events: list[dict[str, JsonValue]]
    memory_admission_candidate_ids: list[str]
    memory_recovery_id: str | None
    memory_trace_id: str | None
    memory_status: str | None
    memory_error_code: str | None
    review_fact_persistence_failed: bool
    result_persisted: bool
    correction_memory_saved: bool
    failure_message: str | None


def initial_graph_state(
    identity: WorkflowIdentity,
    document: DocumentReference,
    tenant_id: str,
    trace_id: str | None = None,
) -> GraphState:
    """Create a complete initial state while preserving identifier boundaries."""

    if identity.document_id != document.document_id:
        raise ValueError("Workflow document_id does not match DocumentReference")
    if not tenant_id.strip() or tenant_id != tenant_id.strip():
        raise ValueError("tenant_id must be non-empty and normalized")
    resolved_trace_id = trace_id or identity.run_id.removeprefix("run_")
    if not resolved_trace_id.strip() or len(resolved_trace_id) > 64:
        raise ValueError("trace_id must be a bounded non-empty identifier")
    return GraphState(
        thread_id=identity.thread_id,
        run_id=identity.run_id,
        document_id=identity.document_id,
        tenant_id=tenant_id,
        trace_id=resolved_trace_id,
        storage_uri=document.storage_uri,
        mime_type=document.mime_type,
        checksum=document.checksum,
        status=WorkflowStatus.RECEIVED.value,
        correction_context_retrieved=False,
        correction_context=[],
        reviewed_example_context=None,
        extraction=None,
        validation_route=None,
        validation_issues=[],
        field_decisions=[],
        review_request=None,
        human_correction=None,
        correction_events=[],
        memory_admission_candidate_ids=[],
        memory_recovery_id=None,
        memory_trace_id=None,
        memory_status=None,
        memory_error_code=None,
        review_fact_persistence_failed=False,
        result_persisted=False,
        correction_memory_saved=False,
        failure_message=None,
    )


def identity_from_state(state: GraphState) -> WorkflowIdentity:
    return WorkflowIdentity(
        thread_id=state["thread_id"],
        run_id=state["run_id"],
        document_id=state["document_id"],
    )


def trace_id_from_state(state: GraphState) -> str:
    """Return the persisted root trace or a stable legacy-checkpoint fallback."""

    trace_id = state.get("trace_id")
    return trace_id if trace_id else state["run_id"].removeprefix("run_")


def document_from_state(state: GraphState) -> DocumentReference:
    return DocumentReference(
        document_id=state["document_id"],
        storage_uri=state["storage_uri"],
        mime_type=state["mime_type"],
        checksum=state["checksum"],
    )


def issues_to_state(issues: tuple[ValidationIssue, ...]) -> list[dict[str, JsonValue]]:
    return [
        {"code": item.code, "message": item.message, "field_path": item.field_path}
        for item in issues
    ]


def field_decisions_to_state(
    decisions: tuple[FieldDecision, ...],
) -> list[dict[str, JsonValue]]:
    return [
        {
            "field_path": decision.field_path,
            "current_value": decision.current_value,
            "candidate_values": list(decision.candidate_values),
            "signals": [
                {
                    "field_path": signal.field_path,
                    "rule": signal.rule,
                    "verdict": signal.verdict.value,
                    "score": signal.score,
                    "message": signal.message,
                    "observed_value": signal.observed_value,
                    "expected": signal.expected,
                    "failure_route": signal.failure_route.value,
                }
                for signal in decision.signals
            ],
            "score": decision.score,
            "route": decision.route.value,
            "user_action": decision.user_action,
        }
        for decision in decisions
    ]


def review_request_to_state(request: ReviewRequest) -> dict[str, JsonValue]:
    return {
        "fields": [
            {
                "field_path": field.field_path,
                "current_value": field.current_value,
                "candidate_values": list(field.candidate_values),
                "triggered_rules": list(field.triggered_rules),
                "reasons": list(field.reasons),
                "user_action": field.user_action,
            }
            for field in request.fields
        ],
        "field_bindings": [
            field_binding_evidence_to_state(item)
            for item in request.field_bindings
        ],
        "evidence_sources": [
            {
                "field_path": item.field_path,
                "source_type": item.source_type,
                "source_id": item.source_id,
                "candidate_values": list(item.candidate_values),
                "page_number": item.page_number,
                "bounding_box": (
                    list(item.bounding_box) if item.bounding_box is not None else None
                ),
                "provider_name": item.provider_name,
                "provider_version": item.provider_version,
                "model_version": item.model_version,
                "provider_score": item.provider_score,
                "source_reference": item.source_reference,
                "comparison_outcome": item.comparison_outcome,
                "reason_codes": list(item.reason_codes),
            }
            for item in request.evidence_sources
        ],
    }


def review_request_from_state(payload: dict[str, JsonValue]) -> ReviewRequest:
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list):
        raise HumanCorrectionError("Review request fields must be a list")
    fields: list[ReviewField] = []
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            raise HumanCorrectionError("Review request field must be an object")
        field_path = raw_field.get("field_path")
        current_value = raw_field.get("current_value")
        candidate_values = raw_field.get("candidate_values")
        triggered_rules = raw_field.get("triggered_rules")
        reasons = raw_field.get("reasons")
        user_action = raw_field.get("user_action")
        if field_path is not None and not isinstance(field_path, str):
            raise HumanCorrectionError("Review field_path must be a string or null")
        if not _is_json_value(current_value):
            raise HumanCorrectionError("Review current_value must be JSON-serializable")
        if not isinstance(candidate_values, list) or not all(
            isinstance(item, str) for item in candidate_values
        ):
            raise HumanCorrectionError("Review candidate_values must be a list of strings")
        if not isinstance(triggered_rules, list) or not all(
            isinstance(item, str) for item in triggered_rules
        ):
            raise HumanCorrectionError("Review triggered_rules must be a list of strings")
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise HumanCorrectionError("Review reasons must be a list of strings")
        if not isinstance(user_action, str) or not user_action.strip():
            raise HumanCorrectionError("Review user_action must be a non-empty string")
        fields.append(
            ReviewField(
                field_path=field_path,
                current_value=cast(JsonValue, current_value),
                candidate_values=tuple(cast(list[str], candidate_values)),
                triggered_rules=tuple(cast(list[str], triggered_rules)),
                reasons=tuple(cast(list[str], reasons)),
                user_action=user_action,
            )
        )
    raw_bindings = payload.get("field_bindings", [])
    if not isinstance(raw_bindings, list) or not all(
        isinstance(item, dict) for item in raw_bindings
    ):
        raise HumanCorrectionError("Review field_bindings must be a list of objects")
    raw_evidence_sources = payload.get("evidence_sources", [])
    if not isinstance(raw_evidence_sources, list) or not all(
        isinstance(item, dict) for item in raw_evidence_sources
    ):
        raise HumanCorrectionError("Review evidence_sources must be a list of objects")
    evidence_sources: list[ReviewEvidenceSummary] = []
    for raw_evidence in cast(list[dict[str, JsonValue]], raw_evidence_sources):
        raw_box = raw_evidence.get("bounding_box")
        if raw_box is not None and (
            not isinstance(raw_box, list)
            or len(raw_box) != 4
            or any(
                not isinstance(item, (int, float)) or isinstance(item, bool)
                for item in raw_box
            )
        ):
            raise HumanCorrectionError("Review evidence bounding_box is invalid")
        try:
            evidence_sources.append(
                ReviewEvidenceSummary(
                    field_path=_required_state_string(raw_evidence, "field_path"),
                    source_type=_required_state_string(raw_evidence, "source_type"),
                    source_id=_required_state_string(raw_evidence, "source_id"),
                    candidate_values=tuple(
                        _state_string_list(raw_evidence, "candidate_values")
                    ),
                    page_number=_optional_state_int(raw_evidence, "page_number"),
                    bounding_box=(
                        cast(
                            tuple[float, float, float, float],
                            tuple(float(item) for item in raw_box),
                        )
                        if raw_box is not None
                        else None
                    ),
                    provider_name=_optional_state_string(raw_evidence, "provider_name"),
                    provider_version=_optional_state_string(
                        raw_evidence,
                        "provider_version",
                    ),
                    model_version=_optional_state_string(raw_evidence, "model_version"),
                    provider_score=_optional_state_float(
                        raw_evidence,
                        "provider_score",
                    ),
                    source_reference=_optional_state_string(
                        raw_evidence,
                        "source_reference",
                    ),
                    comparison_outcome=_optional_state_string(
                        raw_evidence,
                        "comparison_outcome",
                    ),
                    reason_codes=tuple(
                        _state_string_list(raw_evidence, "reason_codes")
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            raise HumanCorrectionError("Review evidence source is invalid") from exc
    return ReviewRequest(
        fields=tuple(fields),
        field_bindings=tuple(
            field_binding_evidence_from_state(item)
            for item in cast(list[dict[str, JsonValue]], raw_bindings)
        ),
        evidence_sources=tuple(evidence_sources),
    )


def human_correction_to_state(correction: HumanCorrection) -> dict[str, JsonValue]:
    return {
        "corrected_invoice": (
            dict(correction.corrected_invoice)
            if correction.corrected_invoice is not None
            else None
        ),
        "reviewer_id": correction.reviewer_id,
        "document_type": correction.document_type,
        "fields": [
            {
                "field_path": field.field_path,
                "action": field.action.value,
                "reason": field.reason,
                "rejected_value": field.rejected_value,
            }
            for field in correction.fields
        ],
        "field_bindings": [
            {
                "evidence_id": item.evidence_id,
                "selected_canonical_field_path": item.selected_canonical_field_path,
                "reason": item.reason,
            }
            for item in correction.field_bindings
        ],
    }


def human_correction_from_state(payload: object) -> HumanCorrection:
    if not isinstance(payload, dict):
        raise HumanCorrectionError("Human correction resume payload must be an object")
    corrected_invoice = payload.get("corrected_invoice")
    reviewer_id = payload.get("reviewer_id")
    document_type = payload.get("document_type")
    raw_fields = payload.get("fields")
    raw_bindings = payload.get("field_bindings", [])
    if corrected_invoice is not None and not isinstance(corrected_invoice, dict):
        raise HumanCorrectionError("corrected_invoice must be an object or null")
    if not isinstance(reviewer_id, str) or not reviewer_id.strip():
        raise HumanCorrectionError("reviewer_id must be a non-empty string")
    if document_type is not None and (
        not isinstance(document_type, str) or not document_type.strip()
    ):
        raise HumanCorrectionError("document_type must be a string or null")
    if not isinstance(raw_fields, list):
        raise HumanCorrectionError("Human correction fields must be a list")
    if not isinstance(raw_bindings, list):
        raise HumanCorrectionError("Human correction field_bindings must be a list")
    if not _is_json_value(corrected_invoice):
        raise HumanCorrectionError("corrected_invoice must contain only JSON values")
    fields: list[FieldReviewDecision] = []
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            raise HumanCorrectionError("Human correction field must be an object")
        field_path = raw_field.get("field_path")
        action = raw_field.get("action")
        reason = raw_field.get("reason")
        if not isinstance(field_path, str) or not field_path.strip():
            raise HumanCorrectionError("Correction field_path must be a non-empty string")
        if not isinstance(action, str):
            raise HumanCorrectionError("Review action must be a string")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            raise HumanCorrectionError("Review reason must be a string or null")
        if "rejected_value" not in raw_field:
            raise HumanCorrectionError("Review decision must include rejected_value")
        rejected_value = raw_field["rejected_value"]
        if not _is_json_value(rejected_value):
            raise HumanCorrectionError("rejected_value must be JSON-serializable")
        try:
            fields.append(
                FieldReviewDecision(
                    field_path=field_path,
                    action=HumanReviewAction(action),
                    reason=reason,
                    rejected_value=cast(JsonValue, rejected_value),
                )
            )
        except ValueError as exc:
            raise HumanCorrectionError("Review decision is invalid") from exc
    bindings: list[FieldBindingReviewDecision] = []
    for raw_binding in raw_bindings:
        if not isinstance(raw_binding, dict):
            raise HumanCorrectionError("Human field binding must be an object")
        try:
            bindings.append(
                FieldBindingReviewDecision(
                    evidence_id=_required_state_string(raw_binding, "evidence_id"),
                    selected_canonical_field_path=_required_state_string(
                        raw_binding,
                        "selected_canonical_field_path",
                    ),
                    reason=_required_state_string(raw_binding, "reason"),
                )
            )
        except ValueError as exc:
            raise HumanCorrectionError("Human field binding is invalid") from exc
    if not fields and not bindings:
        raise HumanCorrectionError("Human correction must include at least one decision")
    return HumanCorrection(
        corrected_invoice=(
            cast(dict[str, JsonValue], corrected_invoice)
            if corrected_invoice is not None
            else None
        ),
        fields=tuple(fields),
        reviewer_id=reviewer_id,
        document_type=cast(str | None, document_type),
        field_bindings=tuple(bindings),
    )


def field_binding_evidence_to_state(
    evidence: FieldBindingEvidence,
) -> dict[str, JsonValue]:
    decision = evidence.binding_decision
    return {
        "evidence_id": evidence.evidence_id,
        "document_id": evidence.document_id,
        "page_number": evidence.page_number,
        "image_reference": evidence.image_reference,
        "observed_label": evidence.observed_label,
        "normalized_label": evidence.normalized_label,
        "nearby_text": list(evidence.nearby_text),
        "observed_value_type": evidence.observed_value_type,
        "bounding_box": list(evidence.bounding_box) if evidence.bounding_box else None,
        "context_observations": [
            {
                "text": item.text,
                "normalized_text": item.normalized_text,
                "relation": item.relation.value,
                "distance": item.distance,
            }
            for item in evidence.context_observations
        ],
        "candidate_field_paths": list(evidence.candidate_field_paths),
        "binding_decision": (
            {
                "decision_id": decision.decision_id,
                "evidence_id": decision.evidence_id,
                "document_type": decision.document_type,
                "schema_version": decision.schema_version,
                "status": decision.status.value,
                "selected_canonical_field_path": decision.selected_canonical_field_path,
                "reason_codes": list(decision.reason_codes),
                "catalog_version": decision.catalog_version.value,
                "index_version": (
                    decision.index_version.value if decision.index_version else None
                ),
                "policy_version": decision.policy_version,
                "authority": decision.authority.value,
                "top1_score": decision.top1_score,
                "top2_score": decision.top2_score,
                "score_margin": decision.score_margin,
                "requires_review": decision.requires_review,
            }
            if decision is not None
            else None
        ),
    }


def field_binding_evidence_from_state(
    payload: dict[str, JsonValue],
) -> FieldBindingEvidence:
    raw_box = payload.get("bounding_box")
    if raw_box is not None and (
        not isinstance(raw_box, list)
        or len(raw_box) != 4
        or not all(isinstance(item, int) and not isinstance(item, bool) for item in raw_box)
    ):
        raise HumanCorrectionError("Field binding bounding_box is invalid")
    raw_context = payload.get("context_observations", [])
    if not isinstance(raw_context, list) or not all(
        isinstance(item, dict) for item in raw_context
    ):
        raise HumanCorrectionError("Field binding context observations are invalid")
    raw_decision = payload.get("binding_decision")
    if raw_decision is not None and not isinstance(raw_decision, dict):
        raise HumanCorrectionError("Field binding decision is invalid")
    try:
        decision = (
            FieldBindingDecisionSummary(
                decision_id=_required_state_string(raw_decision, "decision_id"),
                evidence_id=_required_state_string(raw_decision, "evidence_id"),
                document_type=_required_state_string(raw_decision, "document_type"),
                schema_version=_required_state_string(raw_decision, "schema_version"),
                status=FieldBindingStatus(_required_state_string(raw_decision, "status")),
                selected_canonical_field_path=_optional_state_string(
                    raw_decision,
                    "selected_canonical_field_path",
                ),
                reason_codes=tuple(_state_string_list(raw_decision, "reason_codes")),
                catalog_version=FieldSemanticCatalogVersion(
                    _required_state_string(raw_decision, "catalog_version")
                ),
                index_version=(
                    IndexVersion(_required_state_string(raw_decision, "index_version"))
                    if raw_decision.get("index_version") is not None
                    else None
                ),
                policy_version=_required_state_string(raw_decision, "policy_version"),
                authority=FieldBindingDecisionAuthority(
                    _required_state_string(raw_decision, "authority")
                ),
                top1_score=_optional_state_float(raw_decision, "top1_score"),
                top2_score=_optional_state_float(raw_decision, "top2_score"),
                score_margin=_optional_state_float(raw_decision, "score_margin"),
                requires_review=_required_state_bool(raw_decision, "requires_review"),
            )
            if raw_decision is not None
            else None
        )
        return FieldBindingEvidence(
            evidence_id=_required_state_string(payload, "evidence_id"),
            document_id=_required_state_string(payload, "document_id"),
            page_number=_required_state_int(payload, "page_number"),
            image_reference=_required_state_string(payload, "image_reference"),
            observed_label=_required_state_string(payload, "observed_label"),
            normalized_label=_required_state_string(payload, "normalized_label"),
            nearby_text=tuple(_state_string_list(payload, "nearby_text")),
            observed_value_type=_optional_state_string(payload, "observed_value_type"),
            bounding_box=(
                cast(tuple[int, int, int, int], tuple(raw_box))
                if raw_box is not None
                else None
            ),
            context_observations=tuple(
                FieldContextObservation(
                    text=_required_state_string(item, "text"),
                    normalized_text=_required_state_string(item, "normalized_text"),
                    relation=FieldContextRelation(
                        _required_state_string(item, "relation")
                    ),
                    distance=_optional_state_int(item, "distance"),
                )
                for item in cast(list[dict[str, JsonValue]], raw_context)
            ),
            candidate_field_paths=tuple(
                _state_string_list(payload, "candidate_field_paths")
            ),
            binding_decision=decision,
        )
    except (TypeError, ValueError) as exc:
        raise HumanCorrectionError("Field binding evidence is invalid") from exc


def correction_events_to_state(
    events: tuple[CorrectionEvent, ...],
) -> list[dict[str, JsonValue]]:
    return [
        {
            "document_type": item.document_type,
            "field_path": item.field_path,
            "model_value": item.model_value,
            "corrected_value": item.corrected_value,
            "correction_reason": item.correction_reason,
            "vendor_features": dict(item.vendor_features),
            "template_features": dict(item.template_features),
            "document_reference": item.document_reference,
            "image_reference": item.image_reference,
            "schema_version": item.schema_version,
            "created_at": item.created_at.isoformat(),
            "is_reviewed": item.is_reviewed,
            "is_valid": item.is_valid,
        }
        for item in events
    ]


def correction_events_from_state(
    payload: list[dict[str, JsonValue]],
) -> tuple[CorrectionEvent, ...]:
    events: list[CorrectionEvent] = []
    for item in payload:
        document_type = item.get("document_type")
        field_path = item.get("field_path")
        correction_reason = item.get("correction_reason")
        document_reference = item.get("document_reference")
        image_reference = item.get("image_reference")
        schema_version = item.get("schema_version")
        created_at_raw = item.get("created_at")
        is_reviewed = item.get("is_reviewed")
        is_valid = item.get("is_valid")
        vendor_features = item.get("vendor_features")
        template_features = item.get("template_features")
        required_strings = (
            document_type,
            field_path,
            correction_reason,
            document_reference,
            schema_version,
            created_at_raw,
        )
        if not all(isinstance(value, str) for value in required_strings):
            raise HumanCorrectionError("Checkpoint contains an invalid correction event")
        if image_reference is not None and not isinstance(image_reference, str):
            raise HumanCorrectionError("Correction image_reference must be a string or null")
        if not isinstance(is_reviewed, bool) or not isinstance(is_valid, bool):
            raise HumanCorrectionError("Correction validity flags must be booleans")
        if not _is_json_value(item.get("model_value")) or not _is_json_value(
            item.get("corrected_value")
        ):
            raise HumanCorrectionError("Correction values must be JSON-serializable")
        if not isinstance(vendor_features, dict) or not _is_json_value(vendor_features):
            raise HumanCorrectionError("Correction vendor_features must be a JSON object")
        if not isinstance(template_features, dict) or not _is_json_value(template_features):
            raise HumanCorrectionError("Correction template_features must be a JSON object")
        try:
            created_at = datetime.fromisoformat(cast(str, created_at_raw))
        except ValueError as exc:
            raise HumanCorrectionError("Correction created_at must be ISO 8601") from exc
        if created_at.tzinfo is None:
            raise HumanCorrectionError("Correction created_at must include a timezone")
        events.append(
            CorrectionEvent(
                document_type=cast(str, document_type),
                field_path=cast(str, field_path),
                model_value=item.get("model_value"),
                corrected_value=item.get("corrected_value"),
                correction_reason=cast(str, correction_reason),
                vendor_features=cast(dict[str, JsonValue], vendor_features),
                template_features=cast(dict[str, JsonValue], template_features),
                document_reference=cast(str, document_reference),
                image_reference=image_reference,
                schema_version=cast(str, schema_version),
                created_at=created_at,
                is_reviewed=is_reviewed,
                is_valid=is_valid,
            )
        )
    return tuple(events)


def reviewed_example_context_to_state(
    context: ReviewedExamplePromptContext | None,
) -> dict[str, JsonValue] | None:
    """Serialize only bounded redacted references, never full retrieval results."""

    if context is None:
        return None

    def references_to_state(
        references: tuple[ReviewedExamplePromptReference, ...],
    ) -> list[JsonValue]:
        return [
            {
                "example_id": item.example_id,
                "document_type": item.document_type,
                "field_path": item.field_path,
                "schema_version": item.schema_version,
                "label_type": item.label_type.value,
                "model_value": item.model_value,
                "reviewed_value": item.reviewed_value,
                "correction_reason": item.correction_reason,
                "index_version": item.index_version.value,
            }
            for item in references
        ]

    return {
        "trace_ids": list(context.trace_ids),
        "verified_correct_examples": references_to_state(
            context.verified_correct_examples
        ),
        "reviewed_correction_examples": references_to_state(
            context.reviewed_correction_examples
        ),
        "reviewed_negative_examples": references_to_state(
            context.reviewed_negative_examples
        ),
        "conflicting_field_paths": list(context.conflicting_field_paths),
        "retrieval_policy_version": context.retrieval_policy_version.value,
    }


def reviewed_example_context_from_state(
    payload: dict[str, JsonValue] | None,
) -> ReviewedExamplePromptContext | None:
    """Restore and validate the minimal reviewed-example checkpoint projection."""

    if payload is None:
        return None

    def references_from_state(
        key: str,
    ) -> tuple[ReviewedExamplePromptReference, ...]:
        raw_references = payload.get(key)
        if not isinstance(raw_references, list):
            raise HumanCorrectionError(f"{key} must be a list")
        references: list[ReviewedExamplePromptReference] = []
        for raw in raw_references:
            if not isinstance(raw, dict):
                raise HumanCorrectionError(f"{key} entries must be objects")
            required = {
                name: raw.get(name)
                for name in (
                    "example_id",
                    "document_type",
                    "field_path",
                    "schema_version",
                    "label_type",
                    "index_version",
                )
            }
            if not all(isinstance(value, str) for value in required.values()):
                raise HumanCorrectionError(
                    "Reviewed-example checkpoint reference is invalid"
                )
            correction_reason = raw.get("correction_reason")
            if correction_reason is not None and not isinstance(correction_reason, str):
                raise HumanCorrectionError(
                    "Reviewed-example correction_reason must be a string or null"
                )
            model_value = raw.get("model_value")
            reviewed_value = raw.get("reviewed_value")
            if not _is_json_value(model_value) or not _is_json_value(reviewed_value):
                raise HumanCorrectionError(
                    "Reviewed-example values must be JSON-serializable"
                )
            try:
                references.append(
                    ReviewedExamplePromptReference(
                        example_id=cast(str, required["example_id"]),
                        document_type=cast(str, required["document_type"]),
                        field_path=cast(str, required["field_path"]),
                        schema_version=cast(str, required["schema_version"]),
                        label_type=ExampleLabelType(
                            cast(str, required["label_type"])
                        ),
                        model_value=cast(JsonValue, model_value),
                        reviewed_value=cast(JsonValue, reviewed_value),
                        correction_reason=correction_reason,
                        index_version=IndexVersion(
                            cast(str, required["index_version"])
                        ),
                    )
                )
            except ValueError as exc:
                raise HumanCorrectionError(
                    "Reviewed-example checkpoint reference is invalid"
                ) from exc
        return tuple(references)

    trace_ids = payload.get("trace_ids", [])
    conflicting_paths = payload.get("conflicting_field_paths")
    policy_version = payload.get("retrieval_policy_version")
    if not isinstance(trace_ids, list) or not all(
        isinstance(item, str) for item in trace_ids
    ):
        raise HumanCorrectionError("trace_ids must be a list of strings")
    if not isinstance(conflicting_paths, list) or not all(
        isinstance(item, str) for item in conflicting_paths
    ):
        raise HumanCorrectionError("conflicting_field_paths must be a list of strings")
    if not isinstance(policy_version, str):
        raise HumanCorrectionError("retrieval_policy_version must be a string")
    try:
        return ReviewedExamplePromptContext(
            trace_ids=tuple(cast(list[str], trace_ids)),
            verified_correct_examples=references_from_state(
                "verified_correct_examples"
            ),
            reviewed_correction_examples=references_from_state(
                "reviewed_correction_examples"
            ),
            reviewed_negative_examples=references_from_state(
                "reviewed_negative_examples"
            ),
            conflicting_field_paths=tuple(cast(list[str], conflicting_paths)),
            retrieval_policy_version=RetrievalPolicyVersion(policy_version),
        )
    except ValueError as exc:
        raise HumanCorrectionError(
            "Reviewed-example checkpoint context is invalid"
        ) from exc


def _required_state_string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{key} must be a non-empty string")
    return value


def _optional_state_string(
    payload: dict[str, JsonValue],
    key: str,
) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} must be a string or null")
    return value


def _state_string_list(payload: dict[str, JsonValue], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a list of strings")
    return cast(list[str], value)


def _required_state_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{key} must be an integer")
    return value


def _optional_state_int(payload: dict[str, JsonValue], key: str) -> int | None:
    value = payload.get(key)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError(f"{key} must be an integer or null")
    return value


def _optional_state_float(payload: dict[str, JsonValue], key: str) -> float | None:
    value = payload.get(key)
    if value is not None and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise TypeError(f"{key} must be a number or null")
    return float(value) if value is not None else None


def _required_state_bool(payload: dict[str, JsonValue], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise TypeError(f"{key} must be a boolean")
    return value


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item) for key, item in value.items()
        )
    return False
