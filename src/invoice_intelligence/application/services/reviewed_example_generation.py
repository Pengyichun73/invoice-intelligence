"""Generate durable reviewed examples from explicit human field decisions."""

import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Callable, Generic, TypeVar, cast

from invoice_intelligence.application.errors import HumanCorrectionError
from invoice_intelligence.application.ports.admission import MemoryAdmissionRepository
from invoice_intelligence.application.ports.examples import ReviewedExampleRepository
from invoice_intelligence.application.ports.memory import (
    CorrectionEventRepository,
    CorrectionScopeResolver,
    StoredCorrectionEvent,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.domain.admission import (
    MemoryAdmissionDecision,
    MemoryAdmissionDecisionAuthority,
    MemoryAdmissionStatus,
)
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.examples import (
    ExampleEvidenceReference,
    ExampleLabelType,
    ModelVersion,
    PromptVersion,
    ReviewedExample,
)
from invoice_intelligence.domain.extraction import ExtractionResult, FieldEvidence
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    HumanCorrection,
    HumanReviewAction,
    JsonValue,
    WorkflowIdentity,
)

InvoiceT = TypeVar("InvoiceT")


@dataclass(frozen=True, slots=True)
class ReviewedExampleGenerationResult:
    """Idempotently stored raw events and non-retrievable admission candidates."""

    correction_events: tuple[StoredCorrectionEvent, ...]
    examples: tuple[ReviewedExample, ...]


class ReviewedExampleGenerationService(Generic[InvoiceT]):
    """Turn review actions into pending candidates without granting retrieval access."""

    def __init__(
        self,
        *,
        correction_event_repository: CorrectionEventRepository,
        example_repository: ReviewedExampleRepository,
        admission_repository: MemoryAdmissionRepository,
        field_semantic_catalog: FieldSemanticCatalog[InvoiceT],
        scope_resolver: CorrectionScopeResolver,
        output_schema: type[InvoiceT],
        schema_version: str,
        model_version: ModelVersion,
        prompt_version: PromptVersion,
        admission_policy_version: str,
        fingerprint_salt: str,
        feature_fingerprint_salt: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        for name, value in (
            ("schema_version", schema_version),
            ("admission_policy_version", admission_policy_version),
            ("fingerprint_salt", fingerprint_salt),
            ("feature_fingerprint_salt", feature_fingerprint_salt),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        self._correction_events = correction_event_repository
        self._examples = example_repository
        self._admissions = admission_repository
        self._field_semantic_catalog = field_semantic_catalog
        self._scope_resolver = scope_resolver
        self._output_schema = output_schema
        self._schema_version = schema_version
        self._model_version = model_version
        self._prompt_version = prompt_version
        self._admission_policy_version = admission_policy_version
        self._fingerprint_salt = fingerprint_salt
        self._feature_fingerprint_salt = feature_fingerprint_salt
        self._clock = clock or (lambda: datetime.now(UTC))

    async def generate(
        self,
        *,
        identity: WorkflowIdentity,
        tenant_id: str,
        document: DocumentReference,
        original_result: ExtractionResult[InvoiceT],
        reviewed_result: ExtractionResult[InvoiceT],
        review: HumanCorrection,
        correction_events: tuple[CorrectionEvent, ...],
    ) -> ReviewedExampleGenerationResult:
        """Persist raw corrections, source feedback, and canonical cases in replay-safe order."""

        normalized_tenant = tenant_id.strip()
        if not normalized_tenant or normalized_tenant != tenant_id:
            raise HumanCorrectionError("tenant_id must be non-empty and normalized")
        if not review.reviewer_id.strip():
            raise HumanCorrectionError("An explicit reviewer_id is required")
        stored_events = await self._correction_events.save_correction_events(
            normalized_tenant,
            identity,
            document,
            correction_events,
        )
        event_by_path = {item.event.field_path: item for item in stored_events}
        corrected_paths = {
            item.field_path
            for item in review.fields
            if item.action is HumanReviewAction.CORRECT
        }
        if corrected_paths != set(event_by_path):
            raise HumanCorrectionError(
                "Correct review actions do not match persisted correction events"
            )

        original_values = self._invoice_values(original_result.invoice)
        reviewed_values = self._invoice_values(reviewed_result.invoice)
        facts = self._scope_resolver.current_facts(
            reviewed_result.invoice or original_result.invoice
        )
        document_type = facts.document_type if facts is not None else review.document_type
        if document_type is None:
            raise HumanCorrectionError("document_type cannot be resolved for reviewed cases")
        if review.document_type is not None and review.document_type != document_type:
            raise HumanCorrectionError("Submitted document_type does not match invoice Schema")
        valid_scopes = {
            (item.document_type, item.field_path)
            for item in self._scope_resolver.resolve(
                self._output_schema,
                self._schema_version,
            )
        }
        catalog_versions = {
            item.binding_decision.catalog_version.value
            for item in original_result.field_binding_evidence
            if item.binding_decision is not None
        }
        if len(catalog_versions) > 1:
            raise HumanCorrectionError(
                "Reviewed extraction contains mixed Field Semantic Catalog versions"
            )
        catalog_version = (
            next(iter(catalog_versions))
            if catalog_versions
            else (
                await self._field_semantic_catalog.prompt_catalog(normalized_tenant)
            ).catalog_version.value
        )
        vendor_fingerprint = self._feature_fingerprint(
            normalized_tenant,
            "vendor",
            facts.vendor_features if facts is not None else {},
        )
        template_fingerprint = self._feature_fingerprint(
            normalized_tenant,
            "template",
            facts.template_features if facts is not None else {},
        )
        evidence_by_path = {
            item.field_path: item for item in original_result.field_evidence
        }
        generated: list[ReviewedExample] = []
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Reviewed-example clock must return a timezone-aware datetime")

        for decision in review.fields:
            if (document_type, decision.field_path) not in valid_scopes:
                raise HumanCorrectionError(
                    f"Review field is outside the active Entity Schema: {decision.field_path}"
                )
            model_value = self._resolve_optional(original_values, decision.field_path)
            reviewed_value: JsonValue = model_value
            source_event_id: str | None = None
            if decision.action is HumanReviewAction.CONFIRM_CORRECT:
                label = ExampleLabelType.CONFIRMED_CORRECT
                reason = decision.reason
            elif decision.action is HumanReviewAction.CORRECT:
                label = ExampleLabelType.CORRECTED
                reviewed_value = self._resolve_required(
                    reviewed_values,
                    decision.field_path,
                )
                reason = decision.reason
                source_event_id = event_by_path[decision.field_path].event_id
            else:
                label = ExampleLabelType.CONFIRMED_INCORRECT
                model_value = decision.rejected_value
                reviewed_value = None
                reason = decision.reason

            evidence = self._evidence_reference(
                document,
                evidence_by_path.get(decision.field_path),
            )
            semantic_payload: dict[str, JsonValue] = {
                "tenant_id": normalized_tenant,
                "document_type": document_type,
                "field_path": decision.field_path,
                "schema_version": self._schema_version,
                "catalog_version": catalog_version,
                "model_version": self._model_version.value,
                "prompt_version": self._prompt_version.value,
                "label_type": label.value,
                "model_value": model_value,
                "reviewed_value": reviewed_value,
                "correction_reason": reason,
                "vendor_fingerprint": vendor_fingerprint,
                "template_fingerprint": template_fingerprint,
            }
            fingerprint = self._tenant_fingerprint(normalized_tenant, semantic_payload)
            source_payload = {
                **semantic_payload,
                "run_id": identity.run_id,
                "document_id": identity.document_id,
                "reviewer_id": review.reviewer_id,
                "source_event_id": source_event_id,
                "evidence_reference": self._evidence_payload(evidence),
            }
            source_feedback_id = sha256(
                f"review-feedback\0{self._canonical(source_payload)}".encode("utf-8")
            ).hexdigest()
            candidate = ReviewedExample(
                example_id=sha256(f"reviewed-example\0{fingerprint}".encode("utf-8")).hexdigest(),
                tenant_id=normalized_tenant,
                source_feedback_id=source_feedback_id,
                source_event_id=source_event_id,
                document_id=identity.document_id,
                run_id=identity.run_id,
                document_type=document_type,
                field_path=decision.field_path,
                schema_version=self._schema_version,
                catalog_version=catalog_version,
                model_version=self._model_version,
                prompt_version=self._prompt_version,
                label_type=label,
                model_value=model_value,
                reviewed_value=reviewed_value,
                correction_reason=reason,
                vendor_fingerprint=vendor_fingerprint,
                template_fingerprint=template_fingerprint,
                evidence_reference=evidence,
                reviewer_id=review.reviewer_id,
                is_reviewed=True,
                is_valid=True,
                created_at=now,
                fingerprint=fingerprint,
                occurrence_count=1,
                last_seen_at=now,
            )
            persisted = await self._examples.upsert(candidate)
            if await self._admissions.get(persisted.tenant_id, persisted.example_id) is None:
                await self._admissions.create_pending(
                    self._pending_admission_decision(persisted)
                )
            generated.append(persisted)
        return ReviewedExampleGenerationResult(
            correction_events=stored_events,
            examples=tuple(generated),
        )

    def _pending_admission_decision(
        self,
        example: ReviewedExample,
    ) -> MemoryAdmissionDecision:
        identity = (
            f"{example.tenant_id}\0{example.example_id}"
            f"\0{self._admission_policy_version}"
        )
        return MemoryAdmissionDecision(
            decision_id=sha256(
                f"memory-admission-pending\0{identity}".encode("utf-8")
            ).hexdigest(),
            tenant_id=example.tenant_id,
            example_id=example.example_id,
            previous_status=None,
            status=MemoryAdmissionStatus.PENDING,
            authority=MemoryAdmissionDecisionAuthority.DETERMINISTIC_POLICY,
            decided_by="service:reviewed-example-generation",
            reason="Explicit review fact requires trusted-memory admission",
            reason_codes=("review_fact_pending_quality_admission",),
            assessment_ids=(),
            conflict_ids=(),
            policy_version=self._admission_policy_version,
            idempotency_key_hash=sha256(
                f"memory-admission-initialize\0{identity}".encode("utf-8")
            ).hexdigest(),
            revision=1,
            decided_at=example.created_at,
        )

    @staticmethod
    def _invoice_values(invoice: object | None) -> dict[str, JsonValue] | None:
        if invoice is None or not hasattr(invoice, "model_dump"):
            return None
        business_invoice = getattr(invoice, "root", None) or invoice
        payload = business_invoice.model_dump(mode="json")
        return cast(dict[str, JsonValue], payload) if isinstance(payload, dict) else None

    @classmethod
    def _resolve_required(
        cls,
        payload: dict[str, JsonValue] | None,
        field_path: str,
    ) -> JsonValue:
        if payload is None:
            raise HumanCorrectionError("Reviewed invoice value is unavailable")
        current: JsonValue = payload
        for segment in field_path.split("."):
            if isinstance(current, Mapping) and segment in current:
                current = cast(JsonValue, current[segment])
            elif isinstance(current, Sequence) and not isinstance(current, str):
                if not segment.isdecimal() or int(segment) >= len(current):
                    raise HumanCorrectionError(f"Invalid reviewed field path: {field_path}")
                current = cast(JsonValue, current[int(segment)])
            else:
                raise HumanCorrectionError(f"Invalid reviewed field path: {field_path}")
        return current

    @classmethod
    def _resolve_optional(
        cls,
        payload: dict[str, JsonValue] | None,
        field_path: str,
    ) -> JsonValue:
        if payload is None:
            return None
        try:
            return cls._resolve_required(payload, field_path)
        except HumanCorrectionError:
            return None

    @staticmethod
    def _evidence_reference(
        document: DocumentReference,
        evidence: FieldEvidence | None,
    ) -> ExampleEvidenceReference:
        page_number = evidence.page_number if evidence is not None else None
        return ExampleEvidenceReference(
            document_reference=f"document:{document.document_id}",
            image_reference=(
                f"document:{document.document_id}#page={page_number}"
                if page_number is not None
                else None
            ),
            page_number=page_number,
            evidence_source=evidence.source.value if evidence is not None else None,
            candidate_values=(
                tuple(item for item in evidence.candidate_values if item.strip())
                if evidence is not None
                else ()
            ),
            readability=evidence.readability.value if evidence is not None else None,
            validation_signals=(
                tuple(item for item in evidence.validation_signals if item.strip())
                if evidence is not None
                else ()
            ),
            ambiguous=evidence.ambiguous if evidence is not None else False,
        )

    def _tenant_fingerprint(
        self,
        tenant_id: str,
        payload: Mapping[str, JsonValue],
    ) -> str:
        tenant_salt = hmac.new(
            self._fingerprint_salt.encode("utf-8"),
            tenant_id.encode("utf-8"),
            sha256,
        ).digest()
        return hmac.new(
            tenant_salt,
            self._canonical(payload).encode("utf-8"),
            sha256,
        ).hexdigest()

    def _feature_fingerprint(
        self,
        tenant_id: str,
        feature_kind: str,
        features: Mapping[str, JsonValue],
    ) -> str | None:
        if not features:
            return None
        digest = sha256(
            (
                f"{self._feature_fingerprint_salt}\0tenant:{tenant_id}"
                f"\0{feature_kind}\0{self._canonical(features)}"
            ).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _evidence_payload(reference: ExampleEvidenceReference) -> dict[str, JsonValue]:
        return {
            "document_reference": reference.document_reference,
            "image_reference": reference.image_reference,
            "page_number": reference.page_number,
            "evidence_source": reference.evidence_source,
            "candidate_values": list(reference.candidate_values),
            "readability": reference.readability,
            "validation_signals": list(reference.validation_signals),
            "ambiguous": reference.ambiguous,
        }

    @staticmethod
    def _canonical(value: object) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
