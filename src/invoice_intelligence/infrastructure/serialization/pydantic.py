"""Pydantic extraction result checkpoint codec."""

from collections.abc import Mapping
from typing import TypeVar, cast

from pydantic import BaseModel, ValidationError

from invoice_intelligence.application.errors import WorkflowError
from invoice_intelligence.application.ports.validation import SchemaInspection
from invoice_intelligence.domain.examples import IndexVersion
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    ExtractionAnomaly,
    ExtractionResult,
    FieldEvidence,
    OCRComparisonOutcome,
    OCRFieldObservation,
    OCRVisionComparison,
    PageQuality,
    RawOCRObservation,
    Readability,
)
from invoice_intelligence.domain.field_semantics import (
    FieldBindingDecisionAuthority,
    FieldBindingDecisionSummary,
    FieldBindingEvidence,
    FieldBindingStatus,
    FieldContextObservation,
    FieldContextRelation,
    FieldSemanticCatalogVersion,
)
from invoice_intelligence.domain.workflow import JsonValue

InvoiceT = TypeVar("InvoiceT")


def field_binding_evidence_to_payload(
    evidence: FieldBindingEvidence,
) -> dict[str, JsonValue]:
    """Serialize a binding summary for checkpoints and PostgreSQL review tasks."""

    return PydanticExtractionStateCodec._field_binding_to_state(evidence)


class PydanticInvoiceSchemaInspector:
    """Enforce the Pydantic business Schema and expose framework-neutral leaf values."""

    def inspect(self, invoice: object | None) -> SchemaInspection:
        if invoice is None or not isinstance(invoice, BaseModel):
            return SchemaInspection(python_values={}, json_values={}, valid=False)
        try:
            type(invoice).model_validate(invoice.model_dump(mode="python"))
        except ValidationError:
            return SchemaInspection(python_values={}, json_values={}, valid=False)
        python_values = self._flatten(invoice.model_dump(mode="python"))
        json_values = cast(
            dict[str, JsonValue],
            self._flatten(invoice.model_dump(mode="json")),
        )
        return SchemaInspection(
            python_values=python_values,
            json_values=json_values,
            valid=True,
        )

    @classmethod
    def _flatten(cls, value: object, prefix: str = "") -> dict[str, object]:
        if isinstance(value, Mapping):
            flattened: dict[str, object] = {}
            for key, item in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                flattened.update(cls._flatten(item, path))
            return flattened
        if isinstance(value, list):
            flattened = {}
            for index, item in enumerate(value):
                path = f"{prefix}.{index}" if prefix else str(index)
                flattened.update(cls._flatten(item, path))
            return flattened
        return {prefix: value}


class PydanticExtractionStateCodec:
    """Keep graph checkpoints primitive while enforcing the invoice schema on load."""

    def dump(self, result: ExtractionResult[InvoiceT]) -> dict[str, JsonValue]:
        if result.invoice is not None and not isinstance(result.invoice, BaseModel):
            raise WorkflowError("Invoice extraction must be a Pydantic BaseModel")
        invoice = (
            cast(BaseModel, result.invoice).model_dump(mode="json")
            if result.invoice is not None
            else None
        )
        return {
            "invoice": cast(JsonValue, invoice),
            "field_evidence": [
                {
                    "field_path": item.field_path,
                    "source": item.source.value,
                    "page_number": item.page_number,
                    "candidate_values": list(item.candidate_values),
                    "readability": item.readability.value,
                    "validation_signals": list(item.validation_signals),
                    "ambiguous": item.ambiguous,
                }
                for item in result.field_evidence
            ],
            "anomalies": [
                {
                    "code": item.code,
                    "message": item.message,
                    "field_path": item.field_path,
                    "page_number": item.page_number,
                }
                for item in result.anomalies
            ],
            "page_quality": [
                {
                    "page_number": item.page_number,
                    "width": item.width,
                    "height": item.height,
                    "clarity_score": item.clarity_score,
                }
                for item in result.page_quality
            ],
            "ocr_observations": [
                {
                    "field_path": item.field_path,
                    "page_number": item.page_number,
                    "candidate_values": list(item.candidate_values),
                    "source_id": item.source_id,
                    "provider_name": item.provider_name,
                    "provider_version": item.provider_version,
                    "model_version": item.model_version,
                    "bounding_box": (
                        list(item.bounding_box) if item.bounding_box is not None else None
                    ),
                    "provider_score": item.provider_score,
                    "source_reference": item.source_reference,
                    "anomalies": list(item.anomalies),
                    "candidate_field_paths": list(item.candidate_field_paths),
                    "binding_status": item.binding_status.value,
                    "binding_reason_codes": list(item.binding_reason_codes),
                }
                for item in result.ocr_observations
            ],
            # Raw OCR lines are request-local; checkpoints retain only bound evidence.
            "raw_ocr_observations": [],
            "ocr_comparisons": [
                {
                    "canonical_field_path": item.canonical_field_path,
                    "vision_candidates": list(item.vision_candidates),
                    "ocr_candidates": list(item.ocr_candidates),
                    "supporting_sources": list(item.supporting_sources),
                    "conflicting_sources": list(item.conflicting_sources),
                    "outcome": item.outcome.value,
                    "reason_codes": list(item.reason_codes),
                    "review_required": item.review_required,
                }
                for item in result.ocr_comparisons
            ],
            "field_binding_evidence": [
                field_binding_evidence_to_payload(item)
                for item in result.field_binding_evidence
            ],
        }

    def load(
        self,
        payload: dict[str, JsonValue],
        output_schema: type[InvoiceT],
    ) -> ExtractionResult[InvoiceT]:
        if not isinstance(output_schema, type) or not issubclass(output_schema, BaseModel):
            raise WorkflowError("Invoice output schema must be a Pydantic BaseModel")
        invoice_payload = payload.get("invoice")
        try:
            invoice = (
                cast(InvoiceT, output_schema.model_validate(invoice_payload))
                if invoice_payload is not None
                else None
            )
            evidence_payload = self._object_list(payload, "field_evidence")
            anomaly_payload = self._object_list(payload, "anomalies")
            quality_payload = self._object_list(payload, "page_quality", required=False)
            ocr_payload = self._object_list(payload, "ocr_observations", required=False)
            raw_ocr_payload = self._object_list(
                payload,
                "raw_ocr_observations",
                required=False,
            )
            comparison_payload = self._object_list(
                payload,
                "ocr_comparisons",
                required=False,
            )
            binding_payload = self._object_list(
                payload,
                "field_binding_evidence",
                required=False,
            )
            evidence = tuple(
                FieldEvidence(
                    field_path=self._required_string(item, "field_path"),
                    source=EvidenceSource(self._required_string(item, "source")),
                    page_number=self._optional_int(item, "page_number"),
                    candidate_values=tuple(self._string_list(item, "candidate_values")),
                    readability=Readability(self._required_string(item, "readability")),
                    validation_signals=tuple(self._string_list(item, "validation_signals")),
                    ambiguous=self._optional_bool(item, "ambiguous", default=False),
                )
                for item in evidence_payload
            )
            anomalies = tuple(
                ExtractionAnomaly(
                    code=self._required_string(item, "code"),
                    message=self._required_string(item, "message"),
                    field_path=self._optional_string(item, "field_path"),
                    page_number=self._optional_int(item, "page_number"),
                )
                for item in anomaly_payload
            )
            page_quality = tuple(
                PageQuality(
                    page_number=self._required_int(item, "page_number"),
                    width=self._required_int(item, "width"),
                    height=self._required_int(item, "height"),
                    clarity_score=self._required_float(item, "clarity_score"),
                )
                for item in quality_payload
            )
            ocr_observations = tuple(
                OCRFieldObservation(
                    field_path=self._required_string(item, "field_path"),
                    page_number=self._optional_int(item, "page_number"),
                    candidate_values=tuple(self._string_list(item, "candidate_values")),
                    source_id=self._string_or_default(item, "source_id", "legacy"),
                    provider_name=self._string_or_default(
                        item,
                        "provider_name",
                        "unknown",
                    ),
                    provider_version=self._string_or_default(
                        item,
                        "provider_version",
                        "unknown",
                    ),
                    model_version=self._string_or_default(
                        item,
                        "model_version",
                        "unknown",
                    ),
                    observed_text=self._string_or_default(item, "observed_text", ""),
                    normalized_text=self._string_or_default(
                        item,
                        "normalized_text",
                        "",
                    ),
                    bounding_box=self._optional_float_box(item, "bounding_box"),
                    provider_score=self._optional_float(item, "provider_score"),
                    source_reference=self._optional_string(item, "source_reference"),
                    anomalies=tuple(
                        self._string_list(item, "anomalies", required=False)
                    ),
                    candidate_field_paths=tuple(
                        self._string_list(
                            item,
                            "candidate_field_paths",
                            required=False,
                        )
                    ),
                    binding_status=FieldBindingStatus(
                        self._string_or_default(
                            item,
                            "binding_status",
                            FieldBindingStatus.ACCEPTED.value,
                        )
                    ),
                    binding_reason_codes=tuple(
                        self._string_list(
                            item,
                            "binding_reason_codes",
                            required=False,
                        )
                    ),
                )
                for item in ocr_payload
            )
            raw_ocr_observations = tuple(
                RawOCRObservation(
                    source_id=self._required_string(item, "source_id"),
                    provider_name=self._required_string(item, "provider_name"),
                    provider_version=self._required_string(item, "provider_version"),
                    model_version=self._required_string(item, "model_version"),
                    page_number=self._required_int(item, "page_number"),
                    observed_text=self._required_string(item, "observed_text"),
                    normalized_text=self._required_string(item, "normalized_text"),
                    bounding_box=self._optional_float_box(item, "bounding_box"),
                    provider_score=self._optional_float(item, "provider_score"),
                    source_reference=self._optional_string(item, "source_reference"),
                    anomalies=tuple(self._string_list(item, "anomalies")),
                )
                for item in raw_ocr_payload
            )
            ocr_comparisons = tuple(
                OCRVisionComparison(
                    canonical_field_path=self._required_string(
                        item,
                        "canonical_field_path",
                    ),
                    vision_candidates=tuple(
                        self._string_list(item, "vision_candidates")
                    ),
                    ocr_candidates=tuple(self._string_list(item, "ocr_candidates")),
                    supporting_sources=tuple(
                        self._string_list(item, "supporting_sources")
                    ),
                    conflicting_sources=tuple(
                        self._string_list(item, "conflicting_sources")
                    ),
                    outcome=OCRComparisonOutcome(
                        self._required_string(item, "outcome")
                    ),
                    reason_codes=tuple(self._string_list(item, "reason_codes")),
                    review_required=self._optional_bool(
                        item,
                        "review_required",
                        default=False,
                    ),
                )
                for item in comparison_payload
            )
            field_binding_evidence = tuple(
                self._field_binding_from_state(item) for item in binding_payload
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise WorkflowError("Checkpoint contains an invalid extraction result") from exc
        return ExtractionResult(
            invoice=invoice,
            field_evidence=evidence,
            anomalies=anomalies,
            page_quality=page_quality,
            raw_ocr_observations=raw_ocr_observations,
            ocr_observations=ocr_observations,
            ocr_comparisons=ocr_comparisons,
            field_binding_evidence=field_binding_evidence,
        )

    @staticmethod
    def _field_binding_to_state(
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
            "bounding_box": (
                list(evidence.bounding_box)
                if evidence.bounding_box is not None
                else None
            ),
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
                    "selected_canonical_field_path": (
                        decision.selected_canonical_field_path
                    ),
                    "reason_codes": list(decision.reason_codes),
                    "catalog_version": decision.catalog_version.value,
                    "index_version": (
                        decision.index_version.value
                        if decision.index_version is not None
                        else None
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

    @classmethod
    def _field_binding_from_state(
        cls,
        payload: dict[str, JsonValue],
    ) -> FieldBindingEvidence:
        raw_box = payload.get("bounding_box")
        if raw_box is not None and (
            not isinstance(raw_box, list)
            or len(raw_box) != 4
            or not all(isinstance(item, int) and not isinstance(item, bool) for item in raw_box)
        ):
            raise TypeError("field binding bounding_box must contain four integers or null")
        raw_context = payload.get("context_observations", [])
        if not isinstance(raw_context, list) or not all(
            isinstance(item, dict) for item in raw_context
        ):
            raise TypeError("field binding context_observations must be objects")
        raw_decision = payload.get("binding_decision")
        if raw_decision is not None and not isinstance(raw_decision, dict):
            raise TypeError("field binding decision must be an object or null")
        decision = (
            FieldBindingDecisionSummary(
                decision_id=cls._required_string(raw_decision, "decision_id"),
                evidence_id=cls._required_string(raw_decision, "evidence_id"),
                document_type=cls._required_string(raw_decision, "document_type"),
                schema_version=cls._required_string(raw_decision, "schema_version"),
                status=FieldBindingStatus(
                    cls._required_string(raw_decision, "status")
                ),
                selected_canonical_field_path=cls._optional_string(
                    raw_decision,
                    "selected_canonical_field_path",
                ),
                reason_codes=tuple(cls._string_list(raw_decision, "reason_codes")),
                catalog_version=FieldSemanticCatalogVersion(
                    cls._required_string(raw_decision, "catalog_version")
                ),
                index_version=(
                    IndexVersion(cls._required_string(raw_decision, "index_version"))
                    if raw_decision.get("index_version") is not None
                    else None
                ),
                policy_version=cls._required_string(raw_decision, "policy_version"),
                authority=FieldBindingDecisionAuthority(
                    cls._required_string(raw_decision, "authority")
                ),
                top1_score=cls._optional_float(raw_decision, "top1_score"),
                top2_score=cls._optional_float(raw_decision, "top2_score"),
                score_margin=cls._optional_float(raw_decision, "score_margin"),
                requires_review=cls._optional_bool(
                    raw_decision,
                    "requires_review",
                    default=False,
                ),
            )
            if raw_decision is not None
            else None
        )
        return FieldBindingEvidence(
            evidence_id=cls._required_string(payload, "evidence_id"),
            document_id=cls._required_string(payload, "document_id"),
            page_number=cls._required_int(payload, "page_number"),
            image_reference=cls._required_string(payload, "image_reference"),
            observed_label=cls._required_string(payload, "observed_label"),
            normalized_label=cls._required_string(payload, "normalized_label"),
            nearby_text=tuple(cls._string_list(payload, "nearby_text")),
            observed_value_type=cls._optional_string(payload, "observed_value_type"),
            bounding_box=(
                cast(tuple[int, int, int, int], tuple(raw_box))
                if raw_box is not None
                else None
            ),
            context_observations=tuple(
                FieldContextObservation(
                    text=cls._required_string(item, "text"),
                    normalized_text=cls._required_string(item, "normalized_text"),
                    relation=FieldContextRelation(
                        cls._required_string(item, "relation")
                    ),
                    distance=cls._optional_int(item, "distance"),
                )
                for item in cast(list[dict[str, JsonValue]], raw_context)
            ),
            candidate_field_paths=tuple(
                cls._string_list(payload, "candidate_field_paths")
            ),
            binding_decision=decision,
        )

    @staticmethod
    def _object_list(
        payload: dict[str, JsonValue],
        key: str,
        *,
        required: bool = True,
    ) -> list[dict[str, JsonValue]]:
        value = payload.get(key, [] if not required else None)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise TypeError(f"{key} must be a list of objects")
        return cast(list[dict[str, JsonValue]], value)

    @staticmethod
    def _required_string(payload: dict[str, JsonValue], key: str) -> str:
        value = payload[key]
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a string")
        return value

    @staticmethod
    def _string_or_default(
        payload: dict[str, JsonValue],
        key: str,
        default: str,
    ) -> str:
        value = payload.get(key, default)
        if not isinstance(value, str):
            raise TypeError(f"{key} must be a string")
        return value

    @staticmethod
    def _optional_string(payload: dict[str, JsonValue], key: str) -> str | None:
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise TypeError(f"{key} must be a string or null")
        return value

    @staticmethod
    def _optional_int(payload: dict[str, JsonValue], key: str) -> int | None:
        value = payload.get(key)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise TypeError(f"{key} must be an integer or null")
        return value

    @staticmethod
    def _required_int(payload: dict[str, JsonValue], key: str) -> int:
        value = payload[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{key} must be an integer")
        return value

    @staticmethod
    def _required_float(payload: dict[str, JsonValue], key: str) -> float:
        value = payload[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{key} must be a number")
        return float(value)

    @staticmethod
    def _optional_float(payload: dict[str, JsonValue], key: str) -> float | None:
        value = payload.get(key)
        if value is not None and (
            not isinstance(value, (int, float)) or isinstance(value, bool)
        ):
            raise TypeError(f"{key} must be a number or null")
        return float(value) if value is not None else None

    @staticmethod
    def _optional_float_box(
        payload: dict[str, JsonValue],
        key: str,
    ) -> tuple[float, float, float, float] | None:
        value = payload.get(key)
        if value is None:
            return None
        if (
            not isinstance(value, list)
            or len(value) != 4
            or any(
                not isinstance(item, (int, float)) or isinstance(item, bool)
                for item in value
            )
        ):
            raise TypeError(f"{key} must contain four numeric coordinates")
        return cast(tuple[float, float, float, float], tuple(float(item) for item in value))

    @staticmethod
    def _optional_bool(
        payload: dict[str, JsonValue],
        key: str,
        *,
        default: bool,
    ) -> bool:
        value = payload.get(key, default)
        if not isinstance(value, bool):
            raise TypeError(f"{key} must be a boolean")
        return value

    @staticmethod
    def _string_list(
        payload: dict[str, JsonValue],
        key: str,
        *,
        required: bool = True,
    ) -> list[str]:
        value = payload.get(key, [] if not required else None)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{key} must be a list of strings")
        return cast(list[str], value)
