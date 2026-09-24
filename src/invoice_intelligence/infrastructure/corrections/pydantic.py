"""Pydantic implementation of schema-valid human correction application."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Callable, TypeVar, cast

from pydantic import BaseModel, ValidationError

from invoice_intelligence.application.errors import HumanCorrectionError
from invoice_intelligence.domain.document import DocumentReference
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    ExtractionAnomaly,
    ExtractionResult,
    FieldEvidence,
    Readability,
)
from invoice_intelligence.domain.workflow import (
    CorrectionEvent,
    FieldReviewDecision,
    HumanCorrection,
    HumanReviewAction,
    JsonValue,
    ReviewRequest,
)

InvoiceT = TypeVar("InvoiceT")


class PydanticHumanCorrectionApplier:
    """Apply explicit field review actions and generate correction audit events."""

    def __init__(
        self,
        *,
        schema_version: str,
        vendor_feature_fields: tuple[str, ...],
        template_feature_fields: tuple[str, ...],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        normalized_version = schema_version.strip()
        if not normalized_version:
            raise ValueError("schema_version must not be empty")
        self._schema_version = normalized_version
        self._vendor_feature_fields = vendor_feature_fields
        self._template_feature_fields = template_feature_fields
        self._clock = clock or (lambda: datetime.now(UTC))

    def apply(
        self,
        result: ExtractionResult[InvoiceT],
        correction: HumanCorrection,
        review_request: ReviewRequest,
        output_schema: type[InvoiceT],
        document: DocumentReference,
    ) -> tuple[ExtractionResult[InvoiceT], tuple[CorrectionEvent, ...]]:
        if not isinstance(output_schema, type) or not issubclass(output_schema, BaseModel):
            raise HumanCorrectionError("Invoice output schema must be a Pydantic BaseModel")

        decision_by_path = self._index_decisions(correction)
        required_paths = {
            field.field_path for field in review_request.fields if field.field_path is not None
        }
        missing_paths = required_paths - decision_by_path.keys()
        if missing_paths:
            missing = ", ".join(sorted(missing_paths))
            raise HumanCorrectionError(f"Human review omitted required fields: {missing}")
        unexpected_paths = decision_by_path.keys() - required_paths
        if unexpected_paths:
            unexpected = ", ".join(sorted(unexpected_paths))
            raise HumanCorrectionError(f"Human review contains unexpected fields: {unexpected}")

        original_data = self._invoice_data(result.invoice)
        corrected_invoice = self._validated_invoice(correction, result, output_schema)
        corrected_business_invoice = (
            self._business_model(corrected_invoice)
            if isinstance(corrected_invoice, BaseModel)
            else None
        )
        corrected_data = (
            cast(
                dict[str, JsonValue],
                corrected_business_invoice.model_dump(mode="json"),
            )
            if corrected_business_invoice is not None
            else None
        )
        document_type = (
            type(corrected_business_invoice).__name__
            if corrected_business_invoice is not None
            else correction.document_type
        )
        if document_type is None:
            raise HumanCorrectionError(
                "document_type is required when no invoice variant is available"
            )
        if correction.document_type is not None and correction.document_type != document_type:
            raise HumanCorrectionError("Submitted document_type does not match invoice Schema")
        if original_data is not None and corrected_data is not None:
            changed_paths = self._changed_paths(original_data, corrected_data)
            authorized_paths = {
                path
                for path, decision in decision_by_path.items()
                if decision.action is HumanReviewAction.CORRECT
            }
            unauthorized = changed_paths - authorized_paths
            if unauthorized:
                changed = ", ".join(sorted(unauthorized))
                raise HumanCorrectionError(
                    f"corrected_invoice changed fields without a correct action: {changed}"
                )
        vendor_features = self._select_features(
            corrected_data or original_data or {},
            self._vendor_feature_fields,
        )
        template_features = self._select_features(
            corrected_data or original_data or {},
            self._template_feature_fields,
        )
        evidence_by_path = {item.field_path: item for item in result.field_evidence}
        review_by_path = {
            item.field_path: item
            for item in review_request.fields
            if item.field_path is not None
        }
        events: list[CorrectionEvent] = []
        replacement_evidence: dict[str, FieldEvidence] = {}
        resolved_paths = {
            path
            for path, decision in decision_by_path.items()
            if decision.action is not HumanReviewAction.CONFIRM_INCORRECT
        }
        anomalies = [
            anomaly
            for anomaly in result.anomalies
            if anomaly.field_path not in resolved_paths
        ]

        for field_path, decision in decision_by_path.items():
            previous_value = self._resolve_optional_path(original_data, field_path)
            review_field = review_by_path[field_path]
            if self._canonical_json(review_field.current_value) != self._canonical_json(
                previous_value
            ):
                raise HumanCorrectionError(
                    f"Review request is stale for field: {field_path}"
                )
            existing_evidence = evidence_by_path.get(field_path)
            page_number = existing_evidence.page_number if existing_evidence else None
            image_reference = (
                f"document:{document.document_id}#page={page_number}"
                if page_number is not None
                else None
            )

            if decision.action is HumanReviewAction.CONFIRM_INCORRECT:
                candidates = set(review_field.candidate_values)
                if existing_evidence is not None:
                    candidates.update(existing_evidence.candidate_values)
                if not self._matches_candidate(
                    decision.rejected_value,
                    previous_value,
                    candidates,
                ):
                    raise HumanCorrectionError(
                        f"Rejected value is not a current model candidate: {field_path}"
                    )
                anomalies.append(
                    ExtractionAnomaly(
                        code="human_rejected_candidate",
                        message="人工已明确否定当前模型候选，仍需提供可验证的正确值。",
                        field_path=field_path,
                        page_number=page_number,
                    )
                )
                replacement_evidence[field_path] = FieldEvidence(
                    field_path=field_path,
                    source=(
                        existing_evidence.source
                        if existing_evidence is not None
                        else EvidenceSource.VISUAL
                    ),
                    page_number=page_number,
                    candidate_values=tuple(sorted(candidates)),
                    readability=(
                        existing_evidence.readability
                        if existing_evidence is not None
                        else Readability.PARTIALLY_READABLE
                    ),
                    validation_signals=tuple(
                        dict.fromkeys(
                            (
                                *(existing_evidence.validation_signals if existing_evidence else ()),
                                "human_rejected_candidate",
                            )
                        )
                    ),
                    ambiguous=True,
                )
                continue

            reviewed_value = previous_value
            if decision.action is HumanReviewAction.CORRECT:
                if corrected_data is None:
                    raise HumanCorrectionError("Correct action requires corrected_invoice")
                reviewed_value = self._resolve_path(corrected_data, field_path)
                if self._canonical_json(reviewed_value) == self._canonical_json(
                    previous_value
                ):
                    raise HumanCorrectionError(
                        f"Correct action must change the model value: {field_path}"
                    )
                events.append(
                    CorrectionEvent(
                        document_type=document_type,
                        field_path=field_path,
                        model_value=previous_value,
                        corrected_value=reviewed_value,
                        correction_reason=cast(str, decision.reason),
                        vendor_features=vendor_features,
                        template_features=template_features,
                        document_reference=f"document:{document.document_id}",
                        image_reference=image_reference,
                        schema_version=self._schema_version,
                        created_at=self._clock(),
                        is_reviewed=True,
                        is_valid=True,
                    )
                )
            replacement_evidence[field_path] = FieldEvidence(
                field_path=field_path,
                source=EvidenceSource.HUMAN_CORRECTION,
                page_number=page_number,
                candidate_values=(self._candidate_text(reviewed_value),),
                readability=Readability.READABLE,
                validation_signals=("human_confirmed", "schema_valid"),
                ambiguous=False,
            )

        evidence = [
            replacement_evidence.get(item.field_path, item) for item in result.field_evidence
        ]
        existing_paths = {item.field_path for item in result.field_evidence}
        evidence.extend(
            item for path, item in replacement_evidence.items() if path not in existing_paths
        )
        return (
            ExtractionResult(
                invoice=cast(InvoiceT, corrected_invoice),
                field_evidence=tuple(evidence),
                anomalies=tuple(anomalies),
                page_quality=result.page_quality,
                raw_ocr_observations=result.raw_ocr_observations,
                ocr_observations=result.ocr_observations,
                ocr_comparisons=result.ocr_comparisons,
                field_binding_evidence=result.field_binding_evidence,
            ),
            tuple(events),
        )

    @staticmethod
    def _index_decisions(
        correction: HumanCorrection,
    ) -> dict[str, FieldReviewDecision]:
        indexed: dict[str, FieldReviewDecision] = {}
        for field in correction.fields:
            field_path = field.field_path.strip()
            if not field_path:
                raise HumanCorrectionError("Correction field_path must not be empty")
            if field_path in indexed:
                raise HumanCorrectionError(f"Duplicate review field_path: {field_path}")
            indexed[field_path] = field
        return indexed

    @staticmethod
    def _validated_invoice(
        correction: HumanCorrection,
        result: ExtractionResult[InvoiceT],
        output_schema: type[InvoiceT],
    ) -> InvoiceT | None:
        if correction.corrected_invoice is None:
            return result.invoice
        try:
            return output_schema.model_validate(dict(correction.corrected_invoice))
        except ValidationError as exc:
            raise HumanCorrectionError(
                "Corrected invoice does not satisfy InvoiceExtraction"
            ) from exc

    @staticmethod
    def _invoice_data(invoice: InvoiceT | None) -> dict[str, JsonValue] | None:
        if invoice is None:
            return None
        if not isinstance(invoice, BaseModel):
            raise HumanCorrectionError("Existing invoice must be a Pydantic BaseModel")
        business_invoice = PydanticHumanCorrectionApplier._business_model(invoice)
        return cast(dict[str, JsonValue], business_invoice.model_dump(mode="json"))

    @staticmethod
    def _business_model(invoice: BaseModel) -> BaseModel:
        root = getattr(invoice, "root", None)
        return root if isinstance(root, BaseModel) else invoice

    @classmethod
    def _select_features(
        cls,
        payload: dict[str, JsonValue],
        field_paths: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        features: dict[str, JsonValue] = {}
        for field_path in field_paths:
            value = cls._resolve_optional_path(payload, field_path)
            if value is not None:
                features[field_path] = value
        return features

    @classmethod
    def _resolve_optional_path(
        cls,
        value: dict[str, JsonValue] | None,
        field_path: str,
    ) -> JsonValue:
        if value is None:
            return None
        try:
            return cls._resolve_path(value, field_path)
        except HumanCorrectionError:
            return None

    @staticmethod
    def _resolve_path(value: JsonValue, field_path: str) -> JsonValue:
        current = value
        for segment in field_path.split("."):
            if isinstance(current, Mapping):
                if segment not in current:
                    raise HumanCorrectionError(f"Unknown correction field_path: {field_path}")
                current = cast(JsonValue, current[segment])
            elif isinstance(current, Sequence) and not isinstance(current, str):
                if not segment.isdecimal():
                    raise HumanCorrectionError(f"Invalid array index in field_path: {field_path}")
                index = int(segment)
                if index >= len(current):
                    raise HumanCorrectionError(f"Array index is out of range: {field_path}")
                current = cast(JsonValue, current[index])
            else:
                raise HumanCorrectionError(f"field_path traverses a scalar value: {field_path}")
        return current

    @classmethod
    def _matches_candidate(
        cls,
        rejected_value: JsonValue,
        current_value: JsonValue,
        candidates: set[str],
    ) -> bool:
        if cls._canonical_json(rejected_value) == cls._canonical_json(current_value):
            return True
        return cls._candidate_text(rejected_value) in candidates

    @classmethod
    def _changed_paths(
        cls,
        before: JsonValue,
        after: JsonValue,
        prefix: str = "",
    ) -> set[str]:
        if isinstance(before, dict) and isinstance(after, dict):
            changed: set[str] = set()
            for key in before.keys() | after.keys():
                path = f"{prefix}.{key}" if prefix else key
                if key not in before or key not in after:
                    changed.add(path)
                    continue
                changed.update(cls._changed_paths(before[key], after[key], path))
            return changed
        if isinstance(before, list) and isinstance(after, list):
            changed = set()
            for index in range(max(len(before), len(after))):
                path = f"{prefix}.{index}" if prefix else str(index)
                if index >= len(before) or index >= len(after):
                    changed.add(path)
                    continue
                changed.update(cls._changed_paths(before[index], after[index], path))
            return changed
        return set() if cls._canonical_json(before) == cls._canonical_json(after) else {prefix}

    @staticmethod
    def _canonical_json(value: JsonValue) -> str:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def _candidate_text(cls, value: JsonValue) -> str:
        return value if isinstance(value, str) else cls._canonical_json(value)
