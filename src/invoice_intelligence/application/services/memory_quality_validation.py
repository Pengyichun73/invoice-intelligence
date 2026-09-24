"""Deterministic quality gates for reviewed-example memory admission."""

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from enum import StrEnum
from hashlib import sha256
from typing import Callable, Generic, Literal, TypeVar

from invoice_intelligence.application.ports.admission import (
    MemoryFieldSchemaInspection,
    MemoryFieldSchemaInspector,
    MemorySupportDiversity,
)
from invoice_intelligence.application.services.extraction_validation import ValidationPolicy
from invoice_intelligence.domain.admission import (
    MemoryAdmissionRecommendation,
    MemoryAssessmentSource,
    MemoryConflictRecord,
    MemoryConflictStatus,
    MemoryQualityAssessment,
    MemoryQualitySignal,
)
from invoice_intelligence.domain.examples import ExampleLabelType, ReviewedExample
from invoice_intelligence.domain.extraction import OCRFieldObservation, PageQuality, Readability
from invoice_intelligence.domain.workflow import JsonValue, SignalVerdict, ValidationSignal

InvoiceT = TypeVar("InvoiceT")

_PROMPT_INJECTION_PATTERN = re.compile(
    r"(?:ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?)"
    r"|system\s+prompt|developer\s+message|<\|(?:im_start|system|assistant)\|>"
    r"|\[INST\]|忽略(?:之前|以上|前面).{0,16}(?:指令|提示)"
    r"|系统提示|开发者消息|执行.{0,8}(?:指令|命令))",
    re.IGNORECASE | re.DOTALL,
)


class FieldRiskLevel(StrEnum):
    """Configured business impact of admitting a field-level memory."""

    LOW = "low"
    STANDARD = "standard"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class OwnershipValidationFacts:
    """Repository-resolved identities used to prove tenant and source ownership."""

    trusted_tenant_id: str
    document_id: str
    document_tenant_id: str | None
    run_id: str
    run_tenant_id: str | None
    run_document_id: str | None

    def __post_init__(self) -> None:
        for name, value in (
            ("trusted_tenant_id", self.trusted_tenant_id),
            ("document_id", self.document_id),
            ("run_id", self.run_id),
        ):
            _require_normalized(name, value)
        for name, value in (
            ("document_tenant_id", self.document_tenant_id),
            ("run_tenant_id", self.run_tenant_id),
            ("run_document_id", self.run_document_id),
        ):
            if value is not None:
                _require_normalized(name, value)


@dataclass(frozen=True, slots=True)
class HistoricalDistributionObservation:
    """An externally calculated distribution risk, never a truth label."""

    is_outlier: bool
    reference_count: int
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.reference_count < 0:
            raise ValueError("Historical reference_count must not be negative")
        _require_unique_normalized("historical reason code", self.reason_codes)


@dataclass(frozen=True, slots=True)
class MemoryQualityValidationContext:
    """Observable facts required by the deterministic admission validator."""

    reviewed_example: ReviewedExample
    ownership: OwnershipValidationFacts
    page_quality: tuple[PageQuality, ...]
    ocr_observations: tuple[OCRFieldObservation, ...]
    field_validation_signals: tuple[ValidationSignal, ...]
    conflicts: tuple[MemoryConflictRecord, ...]
    support_diversity: MemorySupportDiversity
    historical_distribution: HistoricalDistributionObservation | None = None

    def __post_init__(self) -> None:
        page_numbers = tuple(item.page_number for item in self.page_quality)
        if len(page_numbers) != len(set(page_numbers)):
            raise ValueError("Page quality observations must have unique page numbers")
        conflict_ids = tuple(item.conflict_id for item in self.conflicts)
        if len(conflict_ids) != len(set(conflict_ids)):
            raise ValueError("Memory quality context cannot repeat conflicts")


@dataclass(frozen=True, slots=True)
class MemoryQualityValidationPolicy:
    """Configurable memory-specific gates plus shared extraction thresholds."""

    version: str
    active_schema_version: str
    extraction_validation: ValidationPolicy
    min_distinct_documents: int = 2
    min_distinct_templates: int = 1
    min_distinct_reviewers: int = 2
    max_text_length: int = 4_000
    max_control_character_ratio: float = 0.01
    max_repeated_character_run: int = 64
    field_risk_levels: Mapping[str, FieldRiskLevel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_normalized("memory quality policy version", self.version)
        _require_normalized("active Schema version", self.active_schema_version)
        if min(
            self.min_distinct_documents,
            self.min_distinct_templates,
            self.min_distinct_reviewers,
        ) <= 0:
            raise ValueError("Minimum support diversity thresholds must be positive")
        if self.max_text_length <= 0:
            raise ValueError("max_text_length must be positive")
        if not 0.0 <= self.max_control_character_ratio <= 1.0:
            raise ValueError("max_control_character_ratio must be between zero and one")
        if self.max_repeated_character_run < 2:
            raise ValueError("max_repeated_character_run must be at least two")
        for path in self.field_risk_levels:
            _require_normalized("memory quality risk field path", path)

    def risk_for(self, field_path: str) -> FieldRiskLevel:
        return self.field_risk_levels.get(field_path, FieldRiskLevel.STANDARD)


@dataclass(frozen=True, slots=True)
class DeterministicMemoryQualityValidation:
    """Assessment plus immutable deterministic blockers for admission policy."""

    assessment: MemoryQualityAssessment
    blocking_failure_codes: tuple[str, ...]
    model_advisory_may_override: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.assessment.source is not MemoryAssessmentSource.DETERMINISTIC:
            raise ValueError("Deterministic validation requires a deterministic assessment")
        actual = tuple(
            signal.code
            for signal in self.assessment.signals
            if signal.verdict is SignalVerdict.FAILED
        )
        if self.blocking_failure_codes != actual:
            raise ValueError("Blocking codes must contain every deterministic failed signal")

    @property
    def signals(self) -> tuple[MemoryQualitySignal, ...]:
        return self.assessment.signals


class DeterministicMemoryQualityValidator(Generic[InvoiceT]):
    """Assess reviewed facts without model confidence or remote model calls."""

    _REJECTION_CODES = frozenset(
        {
            "source.review_fact_invalid",
            "schema.document_type_missing",
            "schema.version_inactive",
            "schema.field_path_missing",
            "schema.value_type_invalid",
            "ownership.trusted_tenant_mismatch",
            "ownership.document_identity_mismatch",
            "ownership.document_tenant_mismatch",
            "ownership.run_identity_mismatch",
            "ownership.run_tenant_mismatch",
            "ownership.run_document_mismatch",
            "ownership.evidence_document_mismatch",
            "ownership.evidence_image_mismatch",
            "conflict.input_scope_invalid",
        }
    )

    def __init__(
        self,
        *,
        schema_inspector: MemoryFieldSchemaInspector,
        output_schema: type[InvoiceT],
        policy: MemoryQualityValidationPolicy,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._schema_inspector = schema_inspector
        self._output_schema = output_schema
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))

    def validate(
        self,
        context: MemoryQualityValidationContext,
    ) -> DeterministicMemoryQualityValidation:
        """Return replay-stable signals and a non-authoritative recommendation."""

        example = context.reviewed_example
        risk = self._policy.risk_for(example.field_path)
        references = self._evidence_references(example)
        schema_value = (
            example.model_value
            if example.label_type is ExampleLabelType.CONFIRMED_INCORRECT
            else example.reviewed_value
        )
        schema = self._schema_inspector.inspect_field(
            self._output_schema,
            example.document_type,
            example.field_path,
            schema_value,
        )

        signals: list[MemoryQualitySignal] = []
        signals.extend(self._source_signals(example, references))
        signals.extend(self._schema_signals(example, schema, references))
        signals.extend(self._ownership_signals(context, references))
        signals.extend(self._evidence_signals(context, risk, references))
        signals.append(self._vision_consistency_signal(example, risk, references))
        signals.append(self._ocr_consistency_signal(context, risk, references))
        signals.extend(self._business_rule_signals(context, references))
        signals.extend(self._conflict_signals(context, references))
        signals.extend(self._diversity_signals(context.support_diversity))
        signals.append(self._distribution_signal(context.historical_distribution))
        signals.extend(self._text_risk_signals(example, references))

        quality_score = self._quality_score(signals)
        failed_codes = tuple(
            signal.code for signal in signals if signal.verdict is SignalVerdict.FAILED
        )
        recommendation = self._recommendation(
            failed_codes,
            quality_score,
            example.field_path,
        )
        input_fingerprint = self._input_fingerprint(context, risk, signals)
        created_at = self._clock()
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError("Memory quality clock must return a timezone-aware datetime")
        assessment_id = sha256(
            (
                "deterministic-memory-quality\0"
                f"{example.tenant_id}\0{example.example_id}\0"
                f"{self._policy.version}\0{input_fingerprint}\0"
                f"{created_at.isoformat()}"
            ).encode("utf-8")
        ).hexdigest()
        reason_codes = tuple(
            dict.fromkeys(
                signal.code
                for signal in signals
                if signal.verdict is not SignalVerdict.PASSED
            )
        ) or ("deterministic_quality_checks_passed",)
        assessment = MemoryQualityAssessment(
            assessment_id=assessment_id,
            tenant_id=example.tenant_id,
            example_id=example.example_id,
            source=MemoryAssessmentSource.DETERMINISTIC,
            signals=tuple(signals),
            quality_score=quality_score,
            recommendation=recommendation,
            reason_codes=reason_codes,
            policy_version=self._policy.version,
            input_fingerprint=input_fingerprint,
            created_at=created_at,
            model_version=None,
            prompt_version=None,
        )
        return DeterministicMemoryQualityValidation(
            assessment=assessment,
            blocking_failure_codes=failed_codes,
        )

    def _source_signals(
        self,
        example: ReviewedExample,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        return (
            _signal(
                "schema.version_current"
                if example.schema_version == self._policy.active_schema_version
                else "schema.version_inactive",
                (
                    SignalVerdict.PASSED
                    if example.schema_version == self._policy.active_schema_version
                    else SignalVerdict.FAILED
                ),
                (
                    1.0
                    if example.schema_version == self._policy.active_schema_version
                    else 0.0
                ),
                "Example Schema version is current"
                if example.schema_version == self._policy.active_schema_version
                else "Example Schema version is not the active version",
                example.field_path,
                references,
            ),
            _signal(
                "source.explicit_review",
                SignalVerdict.PASSED if example.is_reviewed else SignalVerdict.FAILED,
                1.0 if example.is_reviewed else 0.0,
                "An explicit attributable review action exists"
                if example.is_reviewed
                else "The example has no explicit review action",
                example.field_path,
                references,
            ),
            _signal(
                "source.review_fact_valid"
                if example.is_valid
                else "source.review_fact_invalid",
                SignalVerdict.PASSED if example.is_valid else SignalVerdict.FAILED,
                1.0 if example.is_valid else 0.0,
                "The source review fact is valid"
                if example.is_valid
                else "The source review fact has been disabled or invalidated",
                example.field_path,
                references,
            ),
        )

    @staticmethod
    def _schema_signals(
        example: ReviewedExample,
        inspection: MemoryFieldSchemaInspection,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        document_type_exists = inspection.document_type_exists
        field_path_exists = inspection.field_path_exists
        value_type_valid = inspection.value_type_valid
        type_verdict = SignalVerdict.PASSED
        type_code = "schema.value_type_valid"
        if not value_type_valid:
            type_verdict = (
                SignalVerdict.WARNING
                if example.label_type is ExampleLabelType.CONFIRMED_INCORRECT
                else SignalVerdict.FAILED
            )
            type_code = (
                "schema.invalid_negative_value_type"
                if example.label_type is ExampleLabelType.CONFIRMED_INCORRECT
                else "schema.value_type_invalid"
            )
        return (
            _signal(
                "schema.document_type_exists"
                if document_type_exists
                else "schema.document_type_missing",
                SignalVerdict.PASSED if document_type_exists else SignalVerdict.FAILED,
                1.0 if document_type_exists else 0.0,
                "Document type exists in the active Entity Schema"
                if document_type_exists
                else "Document type is absent from the active Entity Schema",
                example.field_path,
                references,
            ),
            _signal(
                "schema.field_path_exists"
                if field_path_exists
                else "schema.field_path_missing",
                SignalVerdict.PASSED if field_path_exists else SignalVerdict.FAILED,
                1.0 if field_path_exists else 0.0,
                "Field path exists in the active Entity Schema"
                if field_path_exists
                else "Field path is absent from the active Entity Schema",
                example.field_path,
                references,
            ),
            _signal(
                type_code,
                type_verdict,
                1.0 if value_type_valid else 0.0,
                "Reviewed value matches the Entity Schema field type"
                if value_type_valid
                else "Reviewed value does not match the Entity Schema field type",
                example.field_path,
                references,
            ),
        )

    @staticmethod
    def _ownership_signals(
        context: MemoryQualityValidationContext,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        example = context.reviewed_example
        ownership = context.ownership
        checks = (
            (
                "ownership.trusted_tenant",
                "ownership.trusted_tenant_mismatch",
                ownership.trusted_tenant_id == example.tenant_id,
                "Trusted request tenant matches the reviewed example",
            ),
            (
                "ownership.document_identity",
                "ownership.document_identity_mismatch",
                ownership.document_id == example.document_id,
                "Resolved document identity matches the reviewed example",
            ),
            (
                "ownership.document_tenant",
                "ownership.document_tenant_mismatch",
                ownership.document_tenant_id == example.tenant_id,
                "Document belongs to the reviewed example tenant",
            ),
            (
                "ownership.run_identity",
                "ownership.run_identity_mismatch",
                ownership.run_id == example.run_id,
                "Resolved run identity matches the reviewed example",
            ),
            (
                "ownership.run_tenant",
                "ownership.run_tenant_mismatch",
                ownership.run_tenant_id == example.tenant_id,
                "Run belongs to the reviewed example tenant",
            ),
            (
                "ownership.run_document",
                "ownership.run_document_mismatch",
                ownership.run_document_id == example.document_id,
                "Run belongs to the reviewed example document",
            ),
            (
                "ownership.evidence_document",
                "ownership.evidence_document_mismatch",
                example.evidence_reference.document_reference
                == f"document:{example.document_id}",
                "Evidence reference belongs to the reviewed example document",
            ),
            (
                "ownership.evidence_image",
                "ownership.evidence_image_mismatch",
                example.evidence_reference.image_reference is None
                or (
                    example.evidence_reference.page_number is not None
                    and example.evidence_reference.image_reference
                    == (
                        f"document:{example.document_id}"
                        f"#page={example.evidence_reference.page_number}"
                    )
                ),
                "Image reference belongs to the reviewed example document",
            ),
        )
        return tuple(
            _signal(
                passed_code if passed else failed_code,
                SignalVerdict.PASSED if passed else SignalVerdict.FAILED,
                1.0 if passed else 0.0,
                message if passed else f"{message} check failed",
                example.field_path,
                references,
            )
            for passed_code, failed_code, passed, message in checks
        )

    def _evidence_signals(
        self,
        context: MemoryQualityValidationContext,
        risk: FieldRiskLevel,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        example = context.reviewed_example
        evidence = example.evidence_reference
        visual_exists = bool(
            evidence.image_reference
            and evidence.page_number is not None
            and evidence.evidence_source == "visual"
            and evidence.readability not in {None, Readability.MISSING.value}
        )
        signals = [
            _signal(
                "evidence.visual_present"
                if visual_exists
                else "evidence.visual_missing",
                SignalVerdict.PASSED if visual_exists else SignalVerdict.FAILED,
                1.0 if visual_exists else 0.0,
                "Original field-level visual evidence is present"
                if visual_exists
                else "Original field-level visual evidence is missing",
                example.field_path,
                references,
            )
        ]
        readability = evidence.readability
        if readability == Readability.READABLE.value:
            verdict = SignalVerdict.PASSED
            score = 1.0
        elif readability == Readability.PARTIALLY_READABLE.value:
            verdict = (
                SignalVerdict.FAILED
                if risk in {FieldRiskLevel.HIGH, FieldRiskLevel.CRITICAL}
                else SignalVerdict.WARNING
            )
            score = 0.5
        else:
            verdict = SignalVerdict.FAILED
            score = 0.0
        signals.append(
            _signal(
                "evidence.readability",
                verdict,
                score,
                "Field-level visual readability was evaluated",
                example.field_path,
                references,
            )
        )

        page = next(
            (
                item
                for item in context.page_quality
                if item.page_number == evidence.page_number
            ),
            None,
        )
        if page is None:
            signals.append(
                _signal(
                    "evidence.page_quality_missing",
                    SignalVerdict.FAILED,
                    0.0,
                    "The referenced evidence page has no deterministic quality metrics",
                    example.field_path,
                    references,
                )
            )
            return tuple(signals)

        thresholds = self._policy.extraction_validation.resolve(example.field_path)
        clarity_passed = page.clarity_score >= thresholds.min_clarity
        clarity_score = (
            min(1.0, page.clarity_score / thresholds.min_clarity)
            if thresholds.min_clarity > 0
            else 1.0
        )
        signals.append(
            _signal(
                "evidence.image_clarity",
                SignalVerdict.PASSED if clarity_passed else SignalVerdict.FAILED,
                clarity_score,
                "Referenced image clarity meets the configured threshold"
                if clarity_passed
                else "Referenced image clarity is below the configured threshold",
                example.field_path,
                references,
            )
        )
        resolution_score = min(
            1.0,
            page.width / thresholds.min_width,
            page.height / thresholds.min_height,
        )
        resolution_passed = (
            page.width >= thresholds.min_width and page.height >= thresholds.min_height
        )
        signals.append(
            _signal(
                "evidence.image_resolution",
                SignalVerdict.PASSED if resolution_passed else SignalVerdict.FAILED,
                resolution_score,
                "Referenced image resolution meets the configured threshold"
                if resolution_passed
                else "Referenced image resolution is below the configured threshold",
                example.field_path,
                references,
            )
        )
        return tuple(signals)

    def _vision_consistency_signal(
        self,
        example: ReviewedExample,
        risk: FieldRiskLevel,
        references: tuple[str, ...],
    ) -> MemoryQualitySignal:
        candidates = example.evidence_reference.candidate_values
        target = (
            example.model_value
            if example.label_type is ExampleLabelType.CONFIRMED_INCORRECT
            else example.reviewed_value
        )
        if not candidates:
            verdict = (
                SignalVerdict.FAILED
                if risk in {FieldRiskLevel.HIGH, FieldRiskLevel.CRITICAL}
                else SignalVerdict.WARNING
            )
            return _signal(
                "vision.candidates_missing",
                verdict,
                0.0,
                "Vision candidates are unavailable for reviewed-value comparison",
                example.field_path,
                references,
            )
        score = _best_text_match(target, candidates)
        threshold = self._policy.extraction_validation.resolve(
            example.field_path
        ).ocr_match_threshold
        matched = score >= threshold
        if matched:
            verdict = SignalVerdict.PASSED
        elif example.label_type in {
            ExampleLabelType.CONFIRMED_CORRECT,
            ExampleLabelType.CONFIRMED_INCORRECT,
        } or risk in {FieldRiskLevel.HIGH, FieldRiskLevel.CRITICAL}:
            verdict = SignalVerdict.FAILED
        else:
            verdict = SignalVerdict.WARNING
        return _signal(
            "vision.reviewed_value_consistency",
            verdict,
            score,
            "Reviewed fact is consistent with a direct Vision candidate"
            if matched
            else "Reviewed fact differs from the available Vision candidates",
            example.field_path,
            references,
        )

    def _ocr_consistency_signal(
        self,
        context: MemoryQualityValidationContext,
        risk: FieldRiskLevel,
        references: tuple[str, ...],
    ) -> MemoryQualitySignal:
        example = context.reviewed_example
        candidates = tuple(
            candidate
            for observation in context.ocr_observations
            if observation.field_path == example.field_path
            for candidate in observation.candidate_values
            if candidate.strip()
        )
        if not candidates:
            return _signal(
                "ocr.not_available",
                SignalVerdict.PASSED,
                None,
                "No optional OCR observation is available; no score penalty was applied",
                example.field_path,
                references,
            )
        target = (
            example.model_value
            if example.label_type is ExampleLabelType.CONFIRMED_INCORRECT
            else example.reviewed_value
        )
        score = _best_text_match(target, candidates)
        threshold = self._policy.extraction_validation.resolve(
            example.field_path
        ).ocr_match_threshold
        matched = score >= threshold
        verdict = SignalVerdict.PASSED if matched else SignalVerdict.WARNING
        if not matched and risk in {FieldRiskLevel.HIGH, FieldRiskLevel.CRITICAL}:
            verdict = SignalVerdict.FAILED
        return _signal(
            "ocr.reviewed_value_consistency",
            verdict,
            score,
            "Reviewed fact is consistent with optional OCR"
            if matched
            else "Reviewed fact differs from optional OCR candidates",
            example.field_path,
            references,
        )

    @staticmethod
    def _business_rule_signals(
        context: MemoryQualityValidationContext,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        field_path = context.reviewed_example.field_path
        applicable = tuple(
            signal
            for signal in context.field_validation_signals
            if signal.field_path == field_path
            and (
                signal.rule.startswith("business_rule.")
                or signal.rule.startswith("format.")
            )
        )
        if not applicable:
            return (
                _signal(
                    "business_rules.not_applicable",
                    SignalVerdict.PASSED,
                    None,
                    "No applicable persisted business or format rule signal was supplied",
                    field_path,
                    references,
                ),
            )
        return tuple(
            _signal(
                signal.rule,
                signal.verdict,
                signal.score,
                signal.message,
                field_path,
                references,
            )
            for signal in applicable
        )

    @staticmethod
    def _conflict_signals(
        context: MemoryQualityValidationContext,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        example = context.reviewed_example
        invalid_scope = tuple(
            item
            for item in context.conflicts
            if item.tenant_id != example.tenant_id
            or item.document_type != example.document_type
            or item.field_path != example.field_path
            or item.schema_version != example.schema_version
            or example.example_id not in item.example_ids
        )
        open_conflicts = tuple(
            item
            for item in context.conflicts
            if item.status is MemoryConflictStatus.OPEN and item not in invalid_scope
        )
        current_refs = set(references)
        same_evidence = tuple(
            item
            for item in open_conflicts
            if current_refs.intersection(item.evidence_references)
        )
        signals: list[MemoryQualitySignal] = []
        if invalid_scope:
            signals.append(
                _signal(
                    "conflict.input_scope_invalid",
                    SignalVerdict.FAILED,
                    0.0,
                    "Conflict input crosses the reviewed example scope",
                    example.field_path,
                    tuple(f"conflict:{item.conflict_id}" for item in invalid_scope),
                )
            )
        if open_conflicts:
            signals.append(
                _signal(
                    "conflict.same_evidence_open"
                    if same_evidence
                    else "conflict.open_review",
                    SignalVerdict.FAILED,
                    0.0,
                    "Conflicting reviews remain open for the same evidence"
                    if same_evidence
                    else "Conflicting reviews remain open for this reviewed example",
                    example.field_path,
                    tuple(f"conflict:{item.conflict_id}" for item in open_conflicts),
                )
            )
        if not signals:
            signals.append(
                _signal(
                    "conflict.none_open",
                    SignalVerdict.PASSED,
                    1.0,
                    "No open conflicting review was supplied",
                    example.field_path,
                    references,
                )
            )
        return tuple(signals)

    def _diversity_signals(
        self,
        diversity: MemorySupportDiversity,
    ) -> tuple[MemoryQualitySignal, ...]:
        checks = (
            (
                "support.distinct_documents",
                diversity.distinct_documents,
                self._policy.min_distinct_documents,
            ),
            (
                "support.distinct_templates",
                diversity.distinct_templates,
                self._policy.min_distinct_templates,
            ),
            (
                "support.distinct_reviewers",
                diversity.distinct_reviewers,
                self._policy.min_distinct_reviewers,
            ),
        )
        return tuple(
            _signal(
                code,
                SignalVerdict.PASSED if observed >= required else SignalVerdict.WARNING,
                min(1.0, observed / required),
                "Support diversity meets the configured threshold"
                if observed >= required
                else "Support diversity is below the configured threshold",
                None,
                (),
            )
            for code, observed, required in checks
        )

    @staticmethod
    def _distribution_signal(
        observation: HistoricalDistributionObservation | None,
    ) -> MemoryQualitySignal:
        if observation is None or observation.reference_count == 0:
            return _signal(
                "distribution.not_available",
                SignalVerdict.PASSED,
                None,
                "No historical distribution baseline is available; no penalty was applied",
                None,
                (),
            )
        return _signal(
            "distribution.outlier_risk"
            if observation.is_outlier
            else "distribution.within_observed_range",
            SignalVerdict.WARNING if observation.is_outlier else SignalVerdict.PASSED,
            0.5 if observation.is_outlier else 1.0,
            "Value is outside the observed distribution; this is only a risk signal"
            if observation.is_outlier
            else "Value is within the observed historical range",
            None,
            (),
        )

    def _text_risk_signals(
        self,
        example: ReviewedExample,
        references: tuple[str, ...],
    ) -> tuple[MemoryQualitySignal, ...]:
        fragments = self._review_text_fragments(example)
        injection_found = any(_PROMPT_INJECTION_PATTERN.search(text) for text in fragments)
        too_long = any(len(text) > self._policy.max_text_length for text in fragments)
        control_ratio = max((_control_character_ratio(text) for text in fragments), default=0.0)
        repeated = any(
            _has_repeated_character_run(
                text,
                self._policy.max_repeated_character_run,
            )
            for text in fragments
        )
        return (
            _signal(
                "text.prompt_injection_risk",
                SignalVerdict.FAILED if injection_found else SignalVerdict.PASSED,
                0.0 if injection_found else 1.0,
                "Prompt-injection-like text was detected"
                if injection_found
                else "No configured prompt-injection pattern was detected",
                example.field_path,
                references,
            ),
            _signal(
                "text.length_anomaly",
                SignalVerdict.FAILED if too_long else SignalVerdict.PASSED,
                0.0 if too_long else 1.0,
                "A reviewed text fragment exceeds the configured length limit"
                if too_long
                else "Reviewed text lengths are within the configured limit",
                example.field_path,
                references,
            ),
            _signal(
                "text.control_character_anomaly",
                (
                    SignalVerdict.FAILED
                    if control_ratio > self._policy.max_control_character_ratio
                    else SignalVerdict.PASSED
                ),
                max(0.0, 1.0 - control_ratio),
                "Unexpected control characters exceed the configured ratio"
                if control_ratio > self._policy.max_control_character_ratio
                else "Control-character ratio is within the configured limit",
                example.field_path,
                references,
            ),
            _signal(
                "text.repetition_anomaly",
                SignalVerdict.FAILED if repeated else SignalVerdict.PASSED,
                0.0 if repeated else 1.0,
                "An abnormal repeated-character run was detected"
                if repeated
                else "No abnormal repeated-character run was detected",
                example.field_path,
                references,
            ),
        )

    def _recommendation(
        self,
        failed_codes: tuple[str, ...],
        quality_score: float,
        field_path: str,
    ) -> MemoryAdmissionRecommendation:
        if any(code in self._REJECTION_CODES for code in failed_codes):
            return MemoryAdmissionRecommendation.RECOMMEND_REJECTION
        if failed_codes:
            return MemoryAdmissionRecommendation.RECOMMEND_QUARANTINE
        acceptance_threshold = self._policy.extraction_validation.resolve(
            field_path
        ).accept_threshold
        if quality_score < acceptance_threshold:
            return MemoryAdmissionRecommendation.RECOMMEND_QUARANTINE
        return MemoryAdmissionRecommendation.RECOMMEND_APPROVAL

    @staticmethod
    def _quality_score(signals: list[MemoryQualitySignal]) -> float:
        scored = [signal.score for signal in signals if signal.score is not None]
        if not scored:
            return 0.0
        return round(sum(scored) / len(scored), 6)

    def _input_fingerprint(
        self,
        context: MemoryQualityValidationContext,
        risk: FieldRiskLevel,
        signals: list[MemoryQualitySignal],
    ) -> str:
        example = context.reviewed_example
        payload = {
            "example_fingerprint": example.fingerprint,
            "source_feedback_id": example.source_feedback_id,
            "tenant_id": example.tenant_id,
            "document_id": example.document_id,
            "run_id": example.run_id,
            "field_path": example.field_path,
            "schema_version": example.schema_version,
            "risk": risk.value,
            "review_fact": {
                "label_type": example.label_type.value,
                "model_value": example.model_value,
                "reviewed_value": example.reviewed_value,
                "correction_reason": example.correction_reason,
                "reviewer_id": example.reviewer_id,
            },
            "evidence": {
                "document_reference": (
                    example.evidence_reference.document_reference
                ),
                "image_reference": example.evidence_reference.image_reference,
                "page_number": example.evidence_reference.page_number,
                "source": example.evidence_reference.evidence_source,
                "candidate_values": list(
                    example.evidence_reference.candidate_values
                ),
                "readability": example.evidence_reference.readability,
                "ambiguous": example.evidence_reference.ambiguous,
            },
            "ownership": {
                "trusted_tenant_id": context.ownership.trusted_tenant_id,
                "document_id": context.ownership.document_id,
                "document_tenant_id": context.ownership.document_tenant_id,
                "run_id": context.ownership.run_id,
                "run_tenant_id": context.ownership.run_tenant_id,
                "run_document_id": context.ownership.run_document_id,
            },
            "page_quality": [
                {
                    "page_number": item.page_number,
                    "width": item.width,
                    "height": item.height,
                    "clarity_score": item.clarity_score,
                }
                for item in context.page_quality
            ],
            "ocr": [
                {
                    "field_path": item.field_path,
                    "page_number": item.page_number,
                    "candidate_values": list(item.candidate_values),
                }
                for item in context.ocr_observations
            ],
            "conflicts": [
                {"conflict_id": item.conflict_id, "status": item.status.value}
                for item in context.conflicts
            ],
            "support": {
                "documents": context.support_diversity.distinct_documents,
                "templates": context.support_diversity.distinct_templates,
                "reviewers": context.support_diversity.distinct_reviewers,
            },
            "distribution": (
                None
                if context.historical_distribution is None
                else {
                    "is_outlier": context.historical_distribution.is_outlier,
                    "reference_count": context.historical_distribution.reference_count,
                    "reason_codes": list(context.historical_distribution.reason_codes),
                }
            ),
            "signals": [
                {
                    "code": item.code,
                    "verdict": item.verdict.value,
                    "score": item.score,
                }
                for item in signals
            ],
            "policy": {
                "version": self._policy.version,
                "active_schema_version": self._policy.active_schema_version,
                "min_distinct_documents": self._policy.min_distinct_documents,
                "min_distinct_templates": self._policy.min_distinct_templates,
                "min_distinct_reviewers": self._policy.min_distinct_reviewers,
                "max_text_length": self._policy.max_text_length,
                "max_control_character_ratio": (
                    self._policy.max_control_character_ratio
                ),
                "max_repeated_character_run": self._policy.max_repeated_character_run,
            },
        }
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _evidence_references(example: ReviewedExample) -> tuple[str, ...]:
        values = (
            example.evidence_reference.document_reference,
            example.evidence_reference.image_reference,
        )
        return tuple(dict.fromkeys(item for item in values if item is not None))

    @staticmethod
    def _review_text_fragments(example: ReviewedExample) -> tuple[str, ...]:
        values: tuple[object, ...] = (
            example.model_value,
            example.reviewed_value,
            example.correction_reason,
            *example.evidence_reference.candidate_values,
        )
        return tuple(_text(value) for value in values if value is not None)


def _signal(
    code: str,
    verdict: SignalVerdict,
    score: float | None,
    message: str,
    field_path: str | None,
    evidence_references: tuple[str, ...],
) -> MemoryQualitySignal:
    return MemoryQualitySignal(
        code=code,
        source=MemoryAssessmentSource.DETERMINISTIC,
        verdict=verdict,
        score=score,
        message=message,
        field_path=field_path,
        evidence_references=tuple(dict.fromkeys(evidence_references)),
    )


def _best_text_match(value: JsonValue, candidates: tuple[str, ...]) -> float:
    target_text = _text(value)
    target = _normalize(target_text)
    if not target:
        return 0.0
    return max(
        (
            _text_similarity(target_text, candidate)
            for candidate in candidates
            if _normalize(candidate)
        ),
        default=0.0,
    )


def _text_similarity(left: str, right: str) -> float:
    left_decimal = _decimal_value(left)
    right_decimal = _decimal_value(right)
    if left_decimal is not None and right_decimal is not None:
        return 1.0 if left_decimal == right_decimal else 0.0
    return SequenceMatcher(None, _normalize(left), _normalize(right)).ratio()


def _decimal_value(value: str) -> Decimal | None:
    normalized = value.strip().replace(",", "")
    if not normalized or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", normalized):
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def _normalize(value: str) -> str:
    date_normalized = value.replace("年", "").replace("月", "").replace("日", "")
    return "".join(
        character.casefold() for character in date_normalized if character.isalnum()
    )


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _control_character_ratio(value: str) -> float:
    if not value:
        return 0.0
    controls = sum(
        1
        for character in value
        if unicodedata.category(character).startswith("C")
        and character not in {"\n", "\r", "\t"}
    )
    return controls / len(value)


def _has_repeated_character_run(value: str, limit: int) -> bool:
    if len(value) < limit:
        return False
    previous = ""
    run = 0
    for character in value:
        if character == previous:
            run += 1
        else:
            previous = character
            run = 1
        if run >= limit:
            return True
    return False


def _require_normalized(name: str, value: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-empty and normalized")


def _require_unique_normalized(name: str, values: tuple[str, ...]) -> None:
    for value in values:
        _require_normalized(name, value)
    if len(values) != len(set(values)):
        raise ValueError(f"{name} values must be unique")
