"""Deterministic comparison of Vision facts and independently observed OCR text."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
from typing import Generic, TypeVar

from invoice_intelligence.application.ports.observability import (
    OCRComparisonMetric,
    OCRTelemetry,
)
from invoice_intelligence.application.ports.validation import InvoiceSchemaInspector
from invoice_intelligence.application.services.field_semantic_binding import (
    FieldSemanticBindingRequest,
    FieldSemanticBindingService,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.domain.extraction import (
    BoundingBox,
    EvidenceSource,
    ExtractionResult,
    OCRComparisonOutcome,
    OCRFieldObservation,
    OCRProviderStatus,
    OCRVisionComparison,
    RawOCRObservation,
    RawOCRResult,
)
from invoice_intelligence.domain.field_semantics import (
    FieldBindingEvidence,
    FieldBindingStatus,
    FieldContextObservation,
    FieldContextRelation,
    FieldSemanticDefinition,
    canonical_field_path_template,
    normalize_field_label,
)
from invoice_intelligence.domain.json_types import JsonValue

InvoiceT = TypeVar("InvoiceT")
_LABEL_SEPARATOR = re.compile(r"\s*[:=]\s*", re.UNICODE)
_DATE_PATTERN = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$")
_DECIMAL_PATTERN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_PUNCTUATION_TRANSLATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "－": "-",
        "—": "-",
        "–": "-",
        "／": "/",
    }
)


class OCRComparisonRiskLevel(StrEnum):
    """Configured review sensitivity for one canonical field."""

    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class OCRComparisonPolicy:
    """Versioned policy; provider scores remain uncalibrated and source-local."""

    version: str
    provider_score_thresholds: Mapping[str, float] = field(default_factory=dict)
    default_provider_score_threshold: float = 0.0
    covered_field_paths: frozenset[str] = frozenset()
    field_risk_levels: Mapping[str, OCRComparisonRiskLevel] = field(default_factory=dict)
    default_field_risk_level: OCRComparisonRiskLevel = OCRComparisonRiskLevel.STANDARD
    review_risk_levels: frozenset[OCRComparisonRiskLevel] = frozenset(
        {OCRComparisonRiskLevel.HIGH, OCRComparisonRiskLevel.CRITICAL}
    )
    amount_tolerance: Decimal = Decimal("0.01")
    max_context_observations: int = 6

    def __post_init__(self) -> None:
        if not self.version.strip() or self.version != self.version.strip():
            raise ValueError("OCR comparison policy version must be normalized")
        thresholds = dict(self.provider_score_thresholds)
        for provider, threshold in thresholds.items():
            if not provider.strip() or provider != provider.strip():
                raise ValueError("OCR provider threshold keys must be normalized")
            self._require_threshold(threshold)
        self._require_threshold(self.default_provider_score_threshold)
        if any(not path.strip() or path != path.strip() for path in self.covered_field_paths):
            raise ValueError("OCR covered field paths must be normalized")
        risks = dict(self.field_risk_levels)
        if any(not path.strip() or path != path.strip() for path in risks):
            raise ValueError("OCR field risk keys must be normalized")
        if any(not isinstance(risk, OCRComparisonRiskLevel) for risk in risks.values()):
            raise TypeError("OCR field risks must use OCRComparisonRiskLevel")
        if not isinstance(self.default_field_risk_level, OCRComparisonRiskLevel):
            raise TypeError("OCR default field risk must use OCRComparisonRiskLevel")
        if not self.review_risk_levels:
            raise ValueError("OCR comparison review risk levels must not be empty")
        if any(
            not isinstance(risk, OCRComparisonRiskLevel)
            for risk in self.review_risk_levels
        ):
            raise TypeError("OCR review risks must use OCRComparisonRiskLevel")
        if not isinstance(self.amount_tolerance, Decimal):
            raise TypeError("OCR comparison amount tolerance must use Decimal")
        if not self.amount_tolerance.is_finite() or self.amount_tolerance < 0:
            raise ValueError("OCR comparison amount tolerance must be finite and non-negative")
        if self.max_context_observations <= 0:
            raise ValueError("OCR comparison context limit must be positive")
        object.__setattr__(self, "provider_score_thresholds", thresholds)
        object.__setattr__(self, "field_risk_levels", risks)

    @staticmethod
    def _require_threshold(value: float) -> None:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError("OCR provider thresholds must be finite scores from zero to one")

    def covers(self, field_path: str) -> bool:
        return (
            not self.covered_field_paths
            or field_path in self.covered_field_paths
            or canonical_field_path_template(field_path) in self.covered_field_paths
        )

    def requires_vision_only_review(self, field_path: str) -> bool:
        risk = self.field_risk_levels.get(
            field_path,
            self.field_risk_levels.get(
                canonical_field_path_template(field_path),
                self.default_field_risk_level,
            ),
        )
        return risk in self.review_risk_levels

    def accepts_provider_score(self, observation: RawOCRObservation) -> bool:
        threshold = self.provider_score_thresholds.get(
            observation.provider_name,
            self.default_provider_score_threshold,
        )
        if observation.provider_score is None:
            return threshold == 0.0
        return observation.provider_score >= threshold


@dataclass(frozen=True, slots=True)
class _LabelMatch:
    label_text: str
    inline_value: str | None
    candidate_paths: tuple[str, ...]


class _ValueKind(StrEnum):
    AMOUNT = "amount"
    DATE = "date"
    IDENTIFIER = "identifier"
    NAME = "name"
    TEXT = "text"


class DeterministicMultiSourceOCRComparisonService(Generic[InvoiceT]):
    """Bind raw OCR lines, aggregate by field path, and compare without voting."""

    def __init__(
        self,
        *,
        catalog: FieldSemanticCatalog[InvoiceT],
        binding_service: FieldSemanticBindingService[InvoiceT],
        schema_inspector: InvoiceSchemaInspector,
        schema_version: str,
        policy: OCRComparisonPolicy,
        telemetry: OCRTelemetry | None = None,
    ) -> None:
        if not schema_version.strip() or schema_version != schema_version.strip():
            raise ValueError("OCR comparison schema_version must be normalized")
        self._catalog = catalog
        self._binding_service = binding_service
        self._schema_inspector = schema_inspector
        self._schema_version = schema_version
        self._policy = policy
        self._telemetry = telemetry

    async def compare(
        self,
        *,
        tenant_id: str,
        document_id: str,
        document_type: str,
        result: ExtractionResult[InvoiceT],
        raw_results: Sequence[RawOCRResult],
    ) -> ExtractionResult[InvoiceT]:
        """Return the same Vision result enriched only with technical OCR evidence."""

        self._require_scope("tenant_id", tenant_id)
        self._require_scope("document_id", document_id)
        self._require_scope("document_type", document_type)
        all_raw = self._raw_observations(raw_results)
        definition_templates = tuple(
            item
            for item in await self._catalog.list_definitions(
                tenant_id,
                document_type=document_type,
            )
            if item.is_valid and self._policy.covers(item.canonical_field_path)
        )
        definitions = self._runtime_definitions(definition_templates, result)
        definition_by_path = {
            item.canonical_field_path: item for item in definitions
        }
        available_raw = self._available_observations(raw_results)
        bound, unresolved = await self._bind_raw_observations(
            tenant_id=tenant_id,
            document_id=document_id,
            document_type=document_type,
            definitions=definitions,
            observations=available_raw,
        )
        bound = self._deduplicate_bound((*result.ocr_observations, *bound))
        vision = self._vision_candidates(result, definition_by_path)
        self._merge_visual_binding_uncertainty(
            result.field_binding_evidence,
            definition_by_path,
            unresolved,
        )
        unavailable = not raw_results or not any(
            item.status is OCRProviderStatus.AVAILABLE for item in raw_results
        )
        partial_unavailable = any(
            item.status is OCRProviderStatus.UNAVAILABLE for item in raw_results
        ) and not unavailable
        comparisons = self._compare_paths(
            vision=vision,
            observations=bound,
            definitions=definition_by_path,
            unresolved=unresolved,
            unavailable=unavailable,
            partial_unavailable=partial_unavailable,
            provider_anomalies=tuple(
                sorted(
                    {
                        anomaly
                        for raw_result in raw_results
                        for anomaly in raw_result.anomalies
                    }
                )
            ),
        )
        self._record_telemetry(raw_results, bound, comparisons)
        return replace(
            result,
            raw_ocr_observations=all_raw,
            ocr_observations=bound,
            ocr_comparisons=comparisons,
        )

    def _runtime_definitions(
        self,
        definitions: Sequence[FieldSemanticDefinition],
        result: ExtractionResult[InvoiceT],
    ) -> tuple[FieldSemanticDefinition, ...]:
        runtime_paths = tuple(self._schema_inspector.inspect(result.invoice).json_values)
        materialized: list[FieldSemanticDefinition] = []
        for definition in definitions:
            if "*" not in definition.canonical_field_path:
                materialized.append(definition)
                continue
            materialized.extend(
                replace(definition, canonical_field_path=field_path)
                for field_path in runtime_paths
                if canonical_field_path_template(field_path)
                == definition.canonical_field_path
            )
        return tuple(
            sorted(materialized, key=lambda item: item.canonical_field_path)
        )

    def degrade(
        self,
        *,
        result: ExtractionResult[InvoiceT],
        raw_results: Sequence[RawOCRResult],
        reason_code: str,
    ) -> ExtractionResult[InvoiceT]:
        """Persist a deterministic summary when centralized comparison is unavailable."""

        if not reason_code.strip() or reason_code != reason_code.strip():
            raise ValueError("OCR comparison degradation reason must be normalized")
        raw_observations = self._raw_observations(raw_results)
        observations = self._deduplicate_bound(result.ocr_observations)
        vision = self._unscoped_vision_candidates(result)
        ocr_by_path: dict[str, list[OCRFieldObservation]] = {}
        for observation in observations:
            if self._policy.covers(observation.field_path):
                ocr_by_path.setdefault(observation.field_path, []).append(observation)
        unavailable = not any(
            item.status is OCRProviderStatus.AVAILABLE for item in raw_results
        ) and not ocr_by_path
        provider_reasons = {
            anomaly for raw_result in raw_results for anomaly in raw_result.anomalies
        }
        provider_reasons.add(reason_code)
        provider_reasons.add(
            "ocr_comparison.safe_degradation"
            if unavailable
            else "ocr_comparison.field_binding_unavailable"
        )
        comparisons = tuple(
            OCRVisionComparison(
                canonical_field_path=field_path,
                vision_candidates=vision.get(field_path, ()),
                ocr_candidates=self._unique_text(
                    candidate
                    for observation in ocr_by_path.get(field_path, ())
                    for candidate in observation.candidate_values
                ),
                supporting_sources=(),
                conflicting_sources=(),
                outcome=(
                    OCRComparisonOutcome.UNAVAILABLE
                    if unavailable
                    else OCRComparisonOutcome.UNRESOLVED
                ),
                reason_codes=tuple(sorted(provider_reasons)),
                review_required=not unavailable,
            )
            for field_path in sorted(set(vision) | set(ocr_by_path))
        )
        self._record_telemetry(raw_results, observations, comparisons)
        return replace(
            result,
            raw_ocr_observations=raw_observations,
            ocr_observations=observations,
            ocr_comparisons=comparisons,
        )

    async def _bind_raw_observations(
        self,
        *,
        tenant_id: str,
        document_id: str,
        document_type: str,
        definitions: Sequence[FieldSemanticDefinition],
        observations: tuple[RawOCRObservation, ...],
    ) -> tuple[tuple[OCRFieldObservation, ...], dict[str, set[str]]]:
        bound: list[OCRFieldObservation] = []
        unresolved: dict[str, set[str]] = {}
        for observation in observations:
            label = self._match_label(observation.observed_text, definitions)
            if label is None:
                continue
            nearby = self._nearby_observations(observation, observations)
            provisional_value = label.inline_value or self._nearest_value_text(
                observation,
                nearby,
                definitions,
            )
            evidence = self._binding_evidence(
                document_id=document_id,
                observation=observation,
                label=label,
                nearby=nearby,
                provisional_value=provisional_value,
                definitions=definitions,
            )
            try:
                decision = await self._binding_service.bind(
                    FieldSemanticBindingRequest(
                        tenant_id=tenant_id,
                        document_type=document_type,
                        schema_version=self._schema_version,
                        evidence=evidence,
                    )
                )
            except Exception:
                reason_codes = ("ocr_comparison.field_binding_unavailable",)
                self._mark_unresolved(
                    unresolved,
                    label.candidate_paths,
                    reason_codes,
                )
                bound.extend(
                    self._uncertain_binding_observations(
                        observation=observation,
                        label=label,
                        nearby=nearby,
                        definitions=definitions,
                        candidate_paths=label.candidate_paths,
                        binding_status=FieldBindingStatus.UNRESOLVED,
                        reason_codes=reason_codes,
                    )
                )
                continue
            candidate_paths = tuple(
                item.canonical_field_path
                for item in decision.candidates
                if self._policy.covers(item.canonical_field_path)
            )
            if (
                decision.status is not FieldBindingStatus.ACCEPTED
                or decision.selected_canonical_field_path is None
            ):
                unresolved_paths = candidate_paths or label.candidate_paths
                self._mark_unresolved(
                    unresolved,
                    unresolved_paths,
                    decision.reason_codes,
                )
                bound.extend(
                    self._uncertain_binding_observations(
                        observation=observation,
                        label=label,
                        nearby=nearby,
                        definitions=definitions,
                        candidate_paths=unresolved_paths,
                        binding_status=decision.status,
                        reason_codes=decision.reason_codes,
                    )
                )
                continue
            field_path = decision.selected_canonical_field_path
            definition = next(
                (item for item in definitions if item.canonical_field_path == field_path),
                None,
            )
            if definition is None:
                continue
            value_observation, value = self._select_value(
                observation,
                label.inline_value,
                nearby,
                definition,
                definitions,
            )
            anomalies = () if value is not None else ("ocr_value_unresolved",)
            if value is None:
                self._mark_unresolved(
                    unresolved,
                    (field_path,),
                    ("ocr_comparison.value_position_unresolved",),
                )
            bound.append(
                OCRFieldObservation(
                    field_path=field_path,
                    page_number=observation.page_number,
                    candidate_values=(value,) if value is not None else (),
                    source_id=value_observation.source_id,
                    provider_name=value_observation.provider_name,
                    provider_version=value_observation.provider_version,
                    model_version=value_observation.model_version,
                    observed_text=value_observation.observed_text,
                    normalized_text=normalize_ocr_text(value_observation.observed_text),
                    bounding_box=value_observation.bounding_box,
                    provider_score=value_observation.provider_score,
                    source_reference=value_observation.source_reference,
                    anomalies=anomalies,
                    candidate_field_paths=candidate_paths,
                    binding_status=decision.status,
                    binding_reason_codes=decision.reason_codes,
                )
            )
        return self._deduplicate_bound(bound), unresolved

    def _uncertain_binding_observations(
        self,
        *,
        observation: RawOCRObservation,
        label: _LabelMatch,
        nearby: tuple[RawOCRObservation, ...],
        definitions: Sequence[FieldSemanticDefinition],
        candidate_paths: tuple[str, ...],
        binding_status: FieldBindingStatus,
        reason_codes: tuple[str, ...],
    ) -> tuple[OCRFieldObservation, ...]:
        """Keep bounded reviewer candidates without claiming a canonical binding."""

        definition_by_path = {
            item.canonical_field_path: item for item in definitions
        }
        observations: list[OCRFieldObservation] = []
        for field_path in sorted(set(candidate_paths)):
            definition = definition_by_path.get(field_path)
            if definition is None:
                continue
            value_observation, value = self._select_value(
                observation,
                label.inline_value,
                nearby,
                definition,
                definitions,
            )
            anomalies = ["ocr_field_binding_unresolved"]
            if value is None:
                anomalies.append("ocr_value_unresolved")
            observations.append(
                OCRFieldObservation(
                    field_path=field_path,
                    page_number=value_observation.page_number,
                    candidate_values=(value,) if value is not None else (),
                    source_id=value_observation.source_id,
                    provider_name=value_observation.provider_name,
                    provider_version=value_observation.provider_version,
                    model_version=value_observation.model_version,
                    observed_text=value_observation.observed_text,
                    normalized_text=normalize_ocr_text(value_observation.observed_text),
                    bounding_box=value_observation.bounding_box,
                    provider_score=value_observation.provider_score,
                    source_reference=value_observation.source_reference,
                    anomalies=tuple(anomalies),
                    candidate_field_paths=tuple(sorted(set(candidate_paths))),
                    binding_status=binding_status,
                    binding_reason_codes=tuple(sorted(set(reason_codes))),
                )
            )
        return tuple(observations)

    def _compare_paths(
        self,
        *,
        vision: Mapping[str, tuple[str, ...]],
        observations: tuple[OCRFieldObservation, ...],
        definitions: Mapping[str, FieldSemanticDefinition],
        unresolved: Mapping[str, set[str]],
        unavailable: bool,
        partial_unavailable: bool,
        provider_anomalies: tuple[str, ...],
    ) -> tuple[OCRVisionComparison, ...]:
        by_path: dict[str, list[OCRFieldObservation]] = {}
        for observation in observations:
            if observation.field_path in definitions:
                by_path.setdefault(observation.field_path, []).append(observation)
        paths = sorted(set(vision) | set(by_path) | set(unresolved))
        comparisons: list[OCRVisionComparison] = []
        for field_path in paths:
            vision_values = vision.get(field_path, ())
            ocr_items = tuple(by_path.get(field_path, ()))
            ocr_values = self._unique_text(
                value for item in ocr_items for value in item.candidate_values
            )
            reasons = set(unresolved.get(field_path, set()))
            reasons.update(provider_anomalies)
            if partial_unavailable:
                reasons.add("ocr_comparison.provider_partial_unavailable")
            if field_path in unresolved:
                outcome = OCRComparisonOutcome.UNRESOLVED
                reasons.add("ocr_comparison.field_binding_unresolved")
                review_required = True
                supporting: tuple[str, ...] = ()
                conflicting: tuple[str, ...] = ()
            elif unavailable and not ocr_values:
                outcome = OCRComparisonOutcome.UNAVAILABLE
                reasons.update(
                    {
                        "ocr_comparison.ocr_unavailable",
                        "ocr_comparison.safe_degradation",
                    }
                )
                review_required = False
                supporting = ()
                conflicting = ()
            elif vision_values and not ocr_values:
                outcome = OCRComparisonOutcome.VISION_ONLY
                reasons.add("ocr_comparison.vision_only")
                review_required = self._policy.requires_vision_only_review(field_path)
                if review_required:
                    reasons.add("ocr_comparison.field_risk_requires_review")
                supporting = (EvidenceSource.VISUAL.value,)
                conflicting = ()
            elif ocr_values and not vision_values:
                outcome = OCRComparisonOutcome.OCR_ONLY
                reasons.update(
                    {
                        "ocr_comparison.ocr_only",
                        "ocr_comparison.automatic_fill_forbidden",
                    }
                )
                review_required = True
                supporting = self._source_keys(ocr_items)
                conflicting = ()
            else:
                outcome, supporting, conflicting, comparison_reasons = (
                    self._compare_values(
                        field_path,
                        vision_values,
                        ocr_items,
                        definitions[field_path],
                    )
                )
                reasons.update(comparison_reasons)
                review_required = outcome in {
                    OCRComparisonOutcome.CONFLICTING,
                    OCRComparisonOutcome.UNRESOLVED,
                }
            comparisons.append(
                OCRVisionComparison(
                    canonical_field_path=field_path,
                    vision_candidates=vision_values,
                    ocr_candidates=ocr_values,
                    supporting_sources=supporting,
                    conflicting_sources=conflicting,
                    outcome=outcome,
                    reason_codes=tuple(sorted(reasons)),
                    review_required=review_required,
                )
            )
        return tuple(comparisons)

    def _compare_values(
        self,
        field_path: str,
        vision_values: tuple[str, ...],
        ocr_items: tuple[OCRFieldObservation, ...],
        definition: FieldSemanticDefinition,
    ) -> tuple[
        OCRComparisonOutcome,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        kind = self._value_kind(field_path, definition.value_type)
        normalized_vision = tuple(
            self._normalize_comparable(value, kind) for value in vision_values
        )
        valid_vision = tuple(value for value in normalized_vision if value is not None)
        if not valid_vision or len(set(valid_vision)) > 1:
            return (
                OCRComparisonOutcome.UNRESOLVED,
                (),
                (),
                ("ocr_comparison.vision_candidates_unresolved",),
            )
        matched: set[str] = set()
        mismatched: set[str] = set()
        normalization_failed = False
        for item in ocr_items:
            source = self._source_key(item)
            for candidate in item.candidate_values:
                normalized = self._normalize_comparable(candidate, kind)
                if normalized is None:
                    normalization_failed = True
                elif any(
                    self._equivalent(expected, normalized, kind)
                    for expected in valid_vision
                ):
                    matched.add(source)
                else:
                    mismatched.add(source)
        if normalization_failed:
            return (
                OCRComparisonOutcome.UNRESOLVED,
                tuple(sorted(matched)),
                tuple(sorted(mismatched)),
                ("ocr_comparison.value_normalization_unresolved",),
            )
        if mismatched:
            return (
                OCRComparisonOutcome.CONFLICTING,
                tuple(sorted({EvidenceSource.VISUAL.value, *matched})),
                tuple(sorted({EvidenceSource.VISUAL.value, *mismatched})),
                (
                    "ocr_comparison.source_conflict",
                    "ocr_comparison.majority_vote_forbidden",
                ),
            )
        if matched:
            return (
                OCRComparisonOutcome.CORROBORATED,
                tuple(sorted({EvidenceSource.VISUAL.value, *matched})),
                (),
                (
                    "ocr_comparison.corroborated",
                    "ocr_comparison.positive_validation_only",
                ),
            )
        return (
            OCRComparisonOutcome.UNRESOLVED,
            (),
            (),
            ("ocr_comparison.insufficient_comparable_evidence",),
        )

    def _vision_candidates(
        self,
        result: ExtractionResult[InvoiceT],
        definitions: Mapping[str, FieldSemanticDefinition],
    ) -> dict[str, tuple[str, ...]]:
        inspection = self._schema_inspector.inspect(result.invoice)
        candidates: dict[str, list[str]] = {}
        for field_path, value in inspection.json_values.items():
            if field_path in definitions and value is not None:
                candidates.setdefault(field_path, []).append(self._json_text(value))
        for evidence in result.field_evidence:
            if evidence.field_path in definitions:
                candidates.setdefault(evidence.field_path, []).extend(
                    evidence.candidate_values
                )
        return {
            path: self._unique_text(values)
            for path, values in sorted(candidates.items())
            if values
        }

    def _unscoped_vision_candidates(
        self,
        result: ExtractionResult[InvoiceT],
    ) -> dict[str, tuple[str, ...]]:
        inspection = self._schema_inspector.inspect(result.invoice)
        candidates: dict[str, list[str]] = {}
        for field_path, value in inspection.json_values.items():
            if self._policy.covers(field_path) and value is not None:
                candidates.setdefault(field_path, []).append(self._json_text(value))
        for evidence in result.field_evidence:
            if self._policy.covers(evidence.field_path):
                candidates.setdefault(evidence.field_path, []).extend(
                    evidence.candidate_values
                )
        return {
            path: self._unique_text(values)
            for path, values in sorted(candidates.items())
            if values
        }

    def _match_label(
        self,
        text: str,
        definitions: Sequence[FieldSemanticDefinition],
    ) -> _LabelMatch | None:
        normalized = normalize_ocr_text(text)
        parts = _LABEL_SEPARATOR.split(normalized, maxsplit=1)
        label_text = parts[0].strip()
        inline_value = parts[1].strip() if len(parts) == 2 and parts[1].strip() else None
        normalized_label = normalize_field_label(label_text)
        labels_by_text: dict[str, set[str]] = {}
        for definition in definitions:
            for candidate in self._definition_label_texts(definition):
                labels_by_text.setdefault(candidate, set()).add(
                    definition.canonical_field_path
                )
        matched_paths = tuple(sorted(labels_by_text.get(normalized_label, set())))
        if not matched_paths and len(parts) == 1:
            prefix_matches = tuple(sorted(
                (candidate, paths)
                for candidate, paths in labels_by_text.items()
                if normalized_label.startswith(f"{candidate} ")
            ))
            if prefix_matches:
                longest = max(len(candidate) for candidate, _ in prefix_matches)
                selected = tuple(
                    (candidate, paths)
                    for candidate, paths in prefix_matches
                    if len(candidate) == longest
                )
                split = self._split_label_prefix(normalized, selected[0][0])
                if split is None:
                    return None
                label_text, inline_value = split
                matched_paths = tuple(
                    sorted({path for _, paths in selected for path in paths})
                )
        if not matched_paths:
            return None
        return _LabelMatch(label_text, inline_value, matched_paths)

    @staticmethod
    def _split_label_prefix(
        text: str,
        normalized_label: str,
    ) -> tuple[str, str | None] | None:
        """Split a label prefix without using normalized character counts as offsets."""

        end = next(
            (
                index
                for index in range(1, len(text) + 1)
                if normalize_field_label(text[:index]) == normalized_label
            ),
            None,
        )
        if end is None:
            return None
        label_text = text[:end].strip()
        inline_value = text[end:].lstrip(" \t:;=)]}").strip() or None
        return label_text, inline_value

    @staticmethod
    def _definition_label_texts(definition: FieldSemanticDefinition) -> frozenset[str]:
        return frozenset(
            normalize_field_label(value)
            for value in (
                definition.display_name,
                definition.description,
                *(alias.alias_text for alias in definition.aliases),
            )
            if normalize_field_label(value)
        )

    def _binding_evidence(
        self,
        *,
        document_id: str,
        observation: RawOCRObservation,
        label: _LabelMatch,
        nearby: tuple[RawOCRObservation, ...],
        provisional_value: str | None,
        definitions: Sequence[FieldSemanticDefinition],
    ) -> FieldBindingEvidence:
        digest = sha256(
            (
                f"ocr-binding\0{document_id}\0{observation.provider_name}\0"
                f"{observation.source_id}\0{observation.page_number}\0"
                f"{observation.normalized_text}\0{observation.bounding_box}"
            ).encode("utf-8")
        ).hexdigest()
        contexts = tuple(
            self._context_observation(observation, item) for item in nearby
        )
        unique_contexts = tuple(
            dict.fromkeys(
                (item.normalized_text, item.relation, item.distance) for item in contexts
            )
        )
        context_by_key = {
            (item.normalized_text, item.relation, item.distance): item for item in contexts
        }
        return FieldBindingEvidence(
            evidence_id=digest,
            document_id=document_id,
            page_number=observation.page_number,
            image_reference=observation.source_reference or f"ocr:{digest}",
            observed_label=label.label_text,
            normalized_label=normalize_field_label(label.label_text),
            nearby_text=self._unique_text(item.observed_text for item in nearby),
            observed_value_type=self._infer_observed_type(
                provisional_value,
                label.candidate_paths,
                definitions,
            ),
            bounding_box=self._integer_box(observation.bounding_box),
            context_observations=tuple(context_by_key[key] for key in unique_contexts),
            candidate_field_paths=label.candidate_paths,
        )

    def _nearby_observations(
        self,
        anchor: RawOCRObservation,
        observations: Sequence[RawOCRObservation],
    ) -> tuple[RawOCRObservation, ...]:
        scoped = (
            item
            for item in observations
            if item is not anchor
            and bool(item.observed_text.strip())
            and bool(normalize_field_label(item.observed_text))
            and item.page_number == anchor.page_number
            and item.provider_name == anchor.provider_name
            and item.provider_version == anchor.provider_version
            and item.model_version == anchor.model_version
        )
        return tuple(
            sorted(scoped, key=lambda item: self._spatial_key(anchor, item))[
                : self._policy.max_context_observations
            ]
        )

    def _nearest_value_text(
        self,
        anchor: RawOCRObservation,
        nearby: Sequence[RawOCRObservation],
        definitions: Sequence[FieldSemanticDefinition],
    ) -> str | None:
        candidate = next(
            (
                item.observed_text
                for item in nearby
                if self._match_label(item.observed_text, definitions) is None
                and self._is_value_position(anchor.bounding_box, item.bounding_box)
            ),
            None,
        )
        return candidate.strip() if candidate is not None and candidate.strip() else None

    def _select_value(
        self,
        anchor: RawOCRObservation,
        inline_value: str | None,
        nearby: Sequence[RawOCRObservation],
        definition: FieldSemanticDefinition,
        definitions: Sequence[FieldSemanticDefinition],
    ) -> tuple[RawOCRObservation, str | None]:
        kind = self._value_kind(definition.canonical_field_path, definition.value_type)
        if inline_value is not None and self._normalize_comparable(inline_value, kind) is not None:
            return anchor, inline_value
        for candidate in nearby:
            if not self._is_value_position(anchor.bounding_box, candidate.bounding_box):
                continue
            if self._match_label(candidate.observed_text, definitions) is not None:
                continue
            if self._normalize_comparable(candidate.observed_text, kind) is not None:
                return candidate, candidate.observed_text.strip()
        return anchor, None

    @classmethod
    def _merge_visual_binding_uncertainty(
        cls,
        evidence: Sequence[FieldBindingEvidence],
        definitions: Mapping[str, FieldSemanticDefinition],
        unresolved: dict[str, set[str]],
    ) -> None:
        for item in evidence:
            decision = item.binding_decision
            paths = tuple(path for path in item.candidate_field_paths if path in definitions)
            if decision is None:
                cls._mark_unresolved(
                    unresolved,
                    paths,
                    ("ocr_comparison.vision_field_binding_unavailable",),
                )
                continue
            if decision.status is FieldBindingStatus.ACCEPTED:
                continue
            cls._mark_unresolved(
                unresolved,
                paths,
                decision.reason_codes,
            )

    @staticmethod
    def _mark_unresolved(
        unresolved: dict[str, set[str]],
        paths: Sequence[str],
        reasons: Sequence[str],
    ) -> None:
        for path in sorted(set(paths)):
            unresolved.setdefault(path, set()).update(reasons)

    def _available_observations(
        self,
        results: Sequence[RawOCRResult],
    ) -> tuple[RawOCRObservation, ...]:
        return self._sort_raw(
            observation
            for result in results
            if result.status is OCRProviderStatus.AVAILABLE
            for observation in result.observations
            if self._policy.accepts_provider_score(observation)
        )

    def _raw_observations(
        self,
        results: Sequence[RawOCRResult],
    ) -> tuple[RawOCRObservation, ...]:
        return self._sort_raw(
            observation for result in results for observation in result.observations
        )

    @classmethod
    def _sort_raw(
        cls,
        observations: Iterable[RawOCRObservation],
    ) -> tuple[RawOCRObservation, ...]:
        unique: dict[tuple[object, ...], RawOCRObservation] = {}
        for raw_item in observations:
            item = replace(
                raw_item,
                normalized_text=normalize_ocr_text(raw_item.observed_text),
                anomalies=tuple(sorted(set(raw_item.anomalies))),
            )
            key = (
                item.provider_name,
                item.provider_version,
                item.model_version,
                item.source_id,
                item.page_number,
                item.normalized_text,
                item.observed_text,
                item.bounding_box,
                item.provider_score,
                item.source_reference,
                item.anomalies,
            )
            unique[key] = item
        return tuple(
            sorted(
                unique.values(),
                key=cls._raw_key,
            )
        )

    @staticmethod
    def _raw_key(item: RawOCRObservation) -> tuple[object, ...]:
        box = item.bounding_box or (math.inf, math.inf, math.inf, math.inf)
        return (
            item.provider_name,
            item.provider_version,
            item.model_version,
            item.page_number,
            box[1],
            box[0],
            item.source_id,
            item.normalized_text,
            item.observed_text,
            item.provider_score if item.provider_score is not None else -1.0,
            item.source_reference or "",
            item.anomalies,
        )

    @classmethod
    def _deduplicate_bound(
        cls,
        observations: Sequence[OCRFieldObservation],
    ) -> tuple[OCRFieldObservation, ...]:
        unique: dict[tuple[object, ...], OCRFieldObservation] = {}
        for raw_item in observations:
            item = replace(
                raw_item,
                candidate_values=cls._unique_text(raw_item.candidate_values),
                normalized_text=normalize_ocr_text(raw_item.observed_text),
                anomalies=tuple(sorted(set(raw_item.anomalies))),
                candidate_field_paths=tuple(sorted(set(raw_item.candidate_field_paths))),
                binding_reason_codes=tuple(sorted(set(raw_item.binding_reason_codes))),
            )
            key = (
                item.field_path,
                item.provider_name,
                item.provider_version,
                item.model_version,
                item.source_id,
                item.page_number,
                tuple(normalize_ocr_text(value) for value in item.candidate_values),
                item.bounding_box,
                item.observed_text,
                item.normalized_text,
                item.provider_score,
                item.source_reference,
                item.anomalies,
                item.candidate_field_paths,
                item.binding_status,
                item.binding_reason_codes,
            )
            unique[key] = item
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    item.field_path,
                    item.provider_name,
                    item.provider_version,
                    item.model_version,
                    item.page_number or 0,
                    item.source_id,
                    tuple(normalize_ocr_text(value) for value in item.candidate_values),
                    item.observed_text,
                    item.normalized_text,
                    item.provider_score if item.provider_score is not None else -1.0,
                    item.source_reference or "",
                    item.anomalies,
                    item.candidate_field_paths,
                    item.binding_status.value,
                    item.binding_reason_codes,
                ),
            )
        )

    @classmethod
    def _spatial_key(
        cls,
        anchor: RawOCRObservation,
        candidate: RawOCRObservation,
    ) -> tuple[object, ...]:
        if anchor.bounding_box is None or candidate.bounding_box is None:
            return (
                2,
                math.inf,
                *cls._raw_key(candidate),
            )
        relation_priority = 0 if cls._same_line(
            anchor.bounding_box,
            candidate.bounding_box,
        ) else 1
        distance = cls._box_distance(anchor.bounding_box, candidate.bounding_box)
        return (
            relation_priority,
            distance,
            *cls._raw_key(candidate),
        )

    @classmethod
    def _context_observation(
        cls,
        anchor: RawOCRObservation,
        candidate: RawOCRObservation,
    ) -> FieldContextObservation:
        relation = FieldContextRelation.FOLLOWING
        if anchor.bounding_box is not None and candidate.bounding_box is not None:
            if cls._same_line(
                anchor.bounding_box,
                candidate.bounding_box,
            ):
                relation = FieldContextRelation.SAME_LINE
            elif candidate.bounding_box[3] <= anchor.bounding_box[1]:
                relation = FieldContextRelation.PRECEDING
        distance = (
            round(
                cls._box_distance(
                    anchor.bounding_box,
                    candidate.bounding_box,
                )
            )
            if anchor.bounding_box is not None and candidate.bounding_box is not None
            else None
        )
        return FieldContextObservation(
            text=candidate.observed_text.strip(),
            normalized_text=normalize_field_label(candidate.observed_text),
            relation=relation,
            distance=distance,
        )

    @classmethod
    def _is_value_position(
        cls,
        anchor: BoundingBox | None,
        candidate: BoundingBox | None,
    ) -> bool:
        if anchor is None or candidate is None:
            return False
        same_line_right = (
            cls._same_line(anchor, candidate)
            and candidate[0] >= anchor[2]
        )
        below = (
            candidate[1] >= anchor[3]
            and candidate[0] < anchor[2]
            and candidate[2] > anchor[0]
        )
        return same_line_right or below

    @staticmethod
    def _same_line(left: BoundingBox, right: BoundingBox) -> bool:
        overlap = max(0.0, min(left[3], right[3]) - max(left[1], right[1]))
        minimum_height = min(left[3] - left[1], right[3] - right[1])
        return minimum_height > 0 and overlap / minimum_height >= 0.5

    @staticmethod
    def _box_distance(left: BoundingBox, right: BoundingBox) -> float:
        horizontal = max(left[0] - right[2], right[0] - left[2], 0.0)
        vertical = max(left[1] - right[3], right[1] - left[3], 0.0)
        return horizontal + vertical

    @staticmethod
    def _integer_box(value: BoundingBox | None) -> tuple[int, int, int, int] | None:
        if value is None:
            return None
        left, top, right, bottom = (max(0, round(item)) for item in value)
        return (left, top, max(left + 1, right), max(top + 1, bottom))

    @classmethod
    def _infer_observed_type(
        cls,
        value: str | None,
        candidate_paths: Sequence[str],
        definitions: Sequence[FieldSemanticDefinition],
    ) -> str | None:
        if value is None:
            return None
        observed_types = {
            observed_type
            for definition in definitions
            if definition.canonical_field_path in candidate_paths
            for observed_type in (cls._schema_observed_type(value, definition),)
            if observed_type is not None
        }
        return next(iter(observed_types)) if len(observed_types) == 1 else None

    @staticmethod
    def _schema_observed_type(
        value: str,
        definition: FieldSemanticDefinition,
    ) -> str | None:
        try:
            schema = json.loads(definition.value_type)
        except json.JSONDecodeError:
            return None
        schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        if '"format":"date-time"' in schema_text:
            return "datetime" if _normalize_date(value) is not None else None
        if '"format":"date"' in schema_text:
            return "date" if _normalize_date(value) is not None else None
        decimal_value = _normalize_decimal(value)
        if '"type":"integer"' in schema_text:
            return (
                "integer"
                if decimal_value is not None and decimal_value == decimal_value.to_integral_value()
                else None
            )
        if '"type":"number"' in schema_text:
            return "number" if decimal_value is not None else None
        if '"type":"string"' in schema_text:
            return "string" if normalize_ocr_text(value) else None
        return None

    @staticmethod
    def _value_kind(field_path: str, value_type: str) -> _ValueKind:
        lowered = field_path.casefold()
        try:
            schema = json.loads(value_type)
        except json.JSONDecodeError:
            schema = {}
        schema_text = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        if '"format":"date' in schema_text or "date" in lowered:
            return _ValueKind.DATE
        if '"type":"number"' in schema_text or any(
            token in lowered for token in ("amount", "price", "quantity", "tax_rate")
        ):
            return _ValueKind.AMOUNT
        if '"type":"integer"' in schema_text or any(
            token in lowered
            for token in ("number", "code", "tax", "account", "voucher", "po_", "er_")
        ):
            return _ValueKind.IDENTIFIER
        if lowered.endswith("name") or "company" in lowered:
            return _ValueKind.NAME
        return _ValueKind.TEXT

    def _normalize_comparable(
        self,
        value: str,
        kind: _ValueKind,
    ) -> str | Decimal | date | None:
        if kind is _ValueKind.AMOUNT:
            return _normalize_decimal(value)
        if kind is _ValueKind.DATE:
            return _normalize_date(value)
        if kind is _ValueKind.IDENTIFIER:
            normalized = normalize_ocr_text(value).casefold()
            compact = "".join(character for character in normalized if character.isalnum())
            return compact or None
        normalized = normalize_ocr_text(value).casefold()
        if kind is _ValueKind.NAME:
            normalized = " ".join(
                "".join(
                    " " if unicodedata.category(character).startswith(("P", "C")) else character
                    for character in normalized
                ).split()
            )
        return normalized or None

    def _equivalent(
        self,
        left: str | Decimal | date,
        right: str | Decimal | date,
        kind: _ValueKind,
    ) -> bool:
        if kind is _ValueKind.AMOUNT:
            return (
                isinstance(left, Decimal)
                and isinstance(right, Decimal)
                and abs(left - right) <= self._policy.amount_tolerance
            )
        return left == right

    @staticmethod
    def _source_key(item: OCRFieldObservation) -> str:
        return ":".join(
            (
                item.provider_name,
                item.provider_version,
                item.model_version,
                item.source_id,
            )
        )

    @classmethod
    def _source_keys(cls, observations: Sequence[OCRFieldObservation]) -> tuple[str, ...]:
        return tuple(sorted({cls._source_key(item) for item in observations}))

    @staticmethod
    def _unique_text(values: Iterable[str]) -> tuple[str, ...]:
        unique: dict[str, str] = {}
        for value in values:
            stripped = value.strip()
            normalized = normalize_ocr_text(stripped)
            if stripped and normalized:
                unique[normalized] = min(stripped, unique.get(normalized, stripped))
        return tuple(unique[key] for key in sorted(unique))

    @staticmethod
    def _json_text(value: JsonValue) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _require_scope(name: str, value: str) -> None:
        if not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be non-empty and normalized")

    def _record_telemetry(
        self,
        raw_results: Sequence[RawOCRResult],
        observations: Sequence[OCRFieldObservation],
        comparisons: Sequence[OCRVisionComparison],
    ) -> None:
        telemetry = self._telemetry
        if telemetry is None:
            return
        try:
            telemetry.record_comparison(
                OCRComparisonMetric(
                    trace_ids=tuple(
                        sorted(
                            {
                                result.trace_id
                                for result in raw_results
                                if result.trace_id is not None
                            }
                        )
                    ),
                    raw_observation_count=sum(
                        len(result.observations) for result in raw_results
                    ),
                    bound_field_count=len(
                        {
                            item.field_path
                            for item in observations
                            if item.binding_status is FieldBindingStatus.ACCEPTED
                        }
                    ),
                    corroborated_count=sum(
                        item.outcome
                        in {
                            OCRComparisonOutcome.CORROBORATED,
                            OCRComparisonOutcome.CONSISTENT,
                        }
                        for item in comparisons
                    ),
                    conflicting_count=sum(
                        item.outcome is OCRComparisonOutcome.CONFLICTING
                        for item in comparisons
                    ),
                    ocr_only_count=sum(
                        item.outcome is OCRComparisonOutcome.OCR_ONLY
                        for item in comparisons
                    ),
                    vision_only_count=sum(
                        item.outcome is OCRComparisonOutcome.VISION_ONLY
                        for item in comparisons
                    ),
                    unresolved_count=sum(
                        item.outcome
                        in {
                            OCRComparisonOutcome.UNRESOLVED,
                            OCRComparisonOutcome.INSUFFICIENT_EVIDENCE,
                        }
                        for item in comparisons
                    ),
                    unavailable_count=sum(
                        item.outcome is OCRComparisonOutcome.UNAVAILABLE
                        for item in comparisons
                    ),
                    conflict_review_required=any(
                        item.outcome is OCRComparisonOutcome.CONFLICTING
                        and item.review_required
                        for item in comparisons
                    ),
                )
            )
        except Exception:
            return


def normalize_ocr_text(value: str) -> str:
    """Apply shared NFKC, punctuation, and whitespace normalization."""

    normalized = unicodedata.normalize("NFKC", value).translate(_PUNCTUATION_TRANSLATION)
    return " ".join(normalized.split())


def _normalize_decimal(value: str) -> Decimal | None:
    normalized = normalize_ocr_text(value).strip()
    negative = normalized.startswith("(") and normalized.endswith(")")
    normalized = normalized.strip("()")
    normalized = normalized.replace(",", "")
    if normalized.endswith("元"):
        normalized = normalized[:-1]
    normalized = "".join(
        character
        for character in normalized
        if character not in {"¥", "￥", "$", "€", "£"} and not character.isspace()
    )
    percent = normalized.endswith("%")
    if percent:
        normalized = normalized[:-1]
    if not _DECIMAL_PATTERN.fullmatch(normalized):
        return None
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    if negative:
        parsed = -parsed
    if percent:
        parsed /= Decimal("100")
    return parsed.normalize()


def _normalize_date(value: str) -> date | None:
    normalized = "".join(normalize_ocr_text(value).split())
    iso_candidate = normalized[:10]
    try:
        return date.fromisoformat(iso_candidate)
    except ValueError:
        pass
    match = _DATE_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None
