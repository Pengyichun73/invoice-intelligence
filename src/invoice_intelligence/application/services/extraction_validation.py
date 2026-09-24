"""Deterministic field-level multi-signal extraction validation."""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import TypeVar, cast

from invoice_intelligence.application.ports.validation import InvoiceSchemaInspector
from invoice_intelligence.domain.extraction import (
    EvidenceSource,
    ExtractionAnomaly,
    ExtractionResult,
    FieldEvidence,
    OCRComparisonOutcome,
    OCRFieldObservation,
    OCRVisionComparison,
    PageQuality,
    Readability,
)
from invoice_intelligence.domain.field_semantics import canonical_field_path_template
from invoice_intelligence.domain.json_types import JsonValue
from invoice_intelligence.domain.workflow import (
    FieldDecision,
    SignalVerdict,
    ValidationIssue,
    ValidationOutcome,
    ValidationRoute,
    ValidationSignal,
)

InvoiceT = TypeVar("InvoiceT")

_IDENTIFIER_FIELDS = frozenset(
    {
        "attribute_1",
        "batch_code",
        "invoice_number",
        "invoice_unique_code",
        "po_number",
        "voucher_number",
    }
)
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True, slots=True)
class ValidationThresholds:
    """Technical routing thresholds with optional per-field replacement."""

    accept_threshold: float = 0.80
    reject_threshold: float = 0.25
    min_clarity: float = 0.45
    min_width: int = 800
    min_height: int = 800
    ocr_match_threshold: float = 0.85
    amount_tolerance: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        if not 0.0 <= self.reject_threshold < self.accept_threshold <= 1.0:
            raise ValueError("Validation thresholds require 0 <= reject < accept <= 1")
        if not 0.0 <= self.min_clarity <= 1.0:
            raise ValueError("min_clarity must be between zero and one")
        if self.min_width <= 0 or self.min_height <= 0:
            raise ValueError("Minimum image dimensions must be greater than zero")
        if not 0.0 <= self.ocr_match_threshold <= 1.0:
            raise ValueError("ocr_match_threshold must be between zero and one")
        if self.amount_tolerance < 0:
            raise ValueError("amount_tolerance must not be negative")


@dataclass(frozen=True, slots=True)
class ValidationThresholdOverride:
    """Sparse field-specific threshold override."""

    accept_threshold: float | None = None
    reject_threshold: float | None = None
    min_clarity: float | None = None
    min_width: int | None = None
    min_height: int | None = None
    ocr_match_threshold: float | None = None
    amount_tolerance: Decimal | None = None

    def apply(self, defaults: ValidationThresholds) -> ValidationThresholds:
        return ValidationThresholds(
            accept_threshold=(
                self.accept_threshold
                if self.accept_threshold is not None
                else defaults.accept_threshold
            ),
            reject_threshold=(
                self.reject_threshold
                if self.reject_threshold is not None
                else defaults.reject_threshold
            ),
            min_clarity=(
                self.min_clarity if self.min_clarity is not None else defaults.min_clarity
            ),
            min_width=self.min_width if self.min_width is not None else defaults.min_width,
            min_height=self.min_height if self.min_height is not None else defaults.min_height,
            ocr_match_threshold=(
                self.ocr_match_threshold
                if self.ocr_match_threshold is not None
                else defaults.ocr_match_threshold
            ),
            amount_tolerance=(
                self.amount_tolerance
                if self.amount_tolerance is not None
                else defaults.amount_tolerance
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Global thresholds plus exact field-path overrides."""

    defaults: ValidationThresholds
    field_overrides: Mapping[str, ValidationThresholdOverride]

    def resolve(self, field_path: str | None) -> ValidationThresholds:
        normalized_path = field_path or ""
        override = self.field_overrides.get(normalized_path) or self.field_overrides.get(
            canonical_field_path_template(normalized_path)
        )
        return override.apply(self.defaults) if override is not None else self.defaults


class EvidenceBasedExtractionValidator:
    """Combine independent observable signals without model confidence scores."""

    def __init__(
        self,
        policy: ValidationPolicy,
        schema_inspector: InvoiceSchemaInspector,
    ) -> None:
        self._policy = policy
        self._schema_inspector = schema_inspector

    def validate(self, result: ExtractionResult[InvoiceT]) -> ValidationOutcome:
        inspection = self._schema_inspector.inspect(result.invoice)
        python_values = inspection.python_values
        json_values = inspection.json_values
        schema_valid = inspection.valid
        evidence_by_path = {item.field_path: item for item in result.field_evidence}
        ocr_by_path = self._group_ocr(result.ocr_observations)
        comparisons_by_path = self._group_ocr_comparisons(result.ocr_comparisons)
        anomalies_by_path = self._group_anomalies(result.anomalies)
        business_signals = self._business_rule_signals(python_values, json_values)
        paths = (
            set(json_values)
            | set(evidence_by_path)
            | set(ocr_by_path)
            | set(comparisons_by_path)
        )
        paths.update(path for path in anomalies_by_path if path is not None)
        ordered_paths: list[str | None] = list(sorted(paths))
        if not ordered_paths or None in anomalies_by_path:
            ordered_paths.append(None)

        decisions = tuple(
            self._decide_field(
                field_path=field_path,
                current_python=python_values.get(field_path) if field_path is not None else None,
                current_json=json_values.get(field_path) if field_path is not None else None,
                schema_valid=schema_valid,
                known_schema_field=field_path in json_values if field_path is not None else False,
                evidence=evidence_by_path.get(field_path) if field_path is not None else None,
                page_quality=result.page_quality,
                ocr=ocr_by_path.get(field_path, ()) if field_path is not None else (),
                ocr_comparison=(
                    comparisons_by_path.get(field_path)
                    if field_path is not None
                    else None
                ),
                anomalies=anomalies_by_path.get(field_path, ()),
                business_signals=(
                    business_signals.get(field_path, ()) if field_path is not None else ()
                ),
            )
            for field_path in ordered_paths
        )
        return ValidationOutcome(
            route=self._overall_route(decisions),
            field_decisions=decisions,
            issues=self._issues(decisions),
        )

    def _decide_field(
        self,
        *,
        field_path: str | None,
        current_python: object,
        current_json: JsonValue,
        schema_valid: bool,
        known_schema_field: bool,
        evidence: FieldEvidence | None,
        page_quality: tuple[PageQuality, ...],
        ocr: tuple[OCRFieldObservation, ...],
        ocr_comparison: OCRVisionComparison | None,
        anomalies: tuple[ExtractionAnomaly, ...],
        business_signals: tuple[ValidationSignal, ...],
    ) -> FieldDecision:
        thresholds = self._policy.resolve(field_path)
        signals: list[ValidationSignal] = []
        human_confirmed = (
            evidence is not None
            and evidence.source is EvidenceSource.HUMAN_CORRECTION
        )

        schema_passed = schema_valid and (known_schema_field or field_path is None)
        signals.append(
            self._signal(
                field_path,
                "schema_validation",
                passed=schema_passed,
                failure_route=ValidationRoute.REVIEW_REQUIRED,
                message=(
                    "InvoiceExtraction Schema 校验通过"
                    if schema_passed
                    else "结果缺失或字段不属于当前 InvoiceExtraction Schema"
                ),
                observed=current_json,
                expected="符合当前 InvoiceExtraction Schema",
            )
        )

        if field_path is not None:
            presence_passed = current_json is not None or human_confirmed
            signals.append(
                self._signal(
                    field_path,
                    "field_presence",
                    passed=presence_passed,
                    failure_route=ValidationRoute.REVIEW_REQUIRED,
                    message=(
                        "字段存在或缺失状态已由人工确认"
                        if presence_passed
                        else "字段缺失"
                    ),
                    observed=current_json,
                    expected="可从原始文档确认的值；文档未提供时由人工确认 null",
                )
            )

        if not human_confirmed:
            quality = self._quality_for_evidence(evidence, page_quality)
            if quality is not None:
                clarity_passed = quality.clarity_score >= thresholds.min_clarity
                clarity_routing_score = (
                    min(1.0, quality.clarity_score / thresholds.min_clarity)
                    if thresholds.min_clarity > 0
                    else 1.0
                )
                signals.append(
                    ValidationSignal(
                        field_path=field_path,
                        rule="image_clarity",
                        verdict=SignalVerdict.PASSED if clarity_passed else SignalVerdict.FAILED,
                        score=clarity_routing_score,
                        message="图像清晰度达标" if clarity_passed else "图像清晰度不足",
                        observed_value=quality.clarity_score,
                        expected=f">= {thresholds.min_clarity}",
                        failure_route=ValidationRoute.REVIEW_REQUIRED,
                    )
                )
                resolution_score = min(
                    1.0,
                    quality.width / thresholds.min_width,
                    quality.height / thresholds.min_height,
                )
                resolution_passed = (
                    quality.width >= thresholds.min_width
                    and quality.height >= thresholds.min_height
                )
                signals.append(
                    ValidationSignal(
                        field_path=field_path,
                        rule="image_resolution",
                        verdict=(
                            SignalVerdict.PASSED if resolution_passed else SignalVerdict.FAILED
                        ),
                        score=resolution_score,
                        message="图像分辨率达标" if resolution_passed else "图像分辨率不足",
                        observed_value={
                            "width": quality.width,
                            "height": quality.height,
                            "page_number": quality.page_number,
                        },
                        expected=f">= {thresholds.min_width}x{thresholds.min_height}",
                        failure_route=ValidationRoute.REVIEW_REQUIRED,
                    )
                )

            if evidence is not None:
                signals.extend(self._vision_signals(evidence, current_json))
            elif field_path is not None:
                signals.append(
                    ValidationSignal(
                        field_path=field_path,
                        rule="vision_evidence_presence",
                        verdict=SignalVerdict.FAILED,
                        score=0.0,
                        message="字段缺少直接视觉证据",
                        observed_value=None,
                        expected="字段级页面、可读性和直接候选证据",
                        failure_route=ValidationRoute.REVIEW_REQUIRED,
                    )
                )
            if ocr_comparison is not None:
                comparison_signal = self._ocr_comparison_signal(ocr_comparison)
                if comparison_signal is not None:
                    signals.append(comparison_signal)
            elif ocr:
                signals.append(
                    self._ocr_signal(
                        field_path,
                        current_json,
                        evidence,
                        ocr,
                        thresholds.ocr_match_threshold,
                    )
                )
        else:
            signals.append(
                ValidationSignal(
                    field_path=field_path,
                    rule="human_confirmation",
                    verdict=SignalVerdict.PASSED,
                    score=1.0,
                    message="字段已由人工明确确认",
                    observed_value=current_json,
                    expected="人工提交且 Schema 有效",
                    failure_route=ValidationRoute.ACCEPTED,
                )
            )

        for anomaly in anomalies:
            if human_confirmed:
                continue
            signals.append(
                ValidationSignal(
                    field_path=field_path,
                    rule=f"provider_anomaly.{anomaly.code}",
                    verdict=SignalVerdict.FAILED,
                    score=0.0,
                    message=anomaly.message,
                    observed_value=None,
                    expected="无提取异常",
                    failure_route=ValidationRoute.REVIEW_REQUIRED,
                )
            )

        if not human_confirmed:
            signals.extend(business_signals)
            signals.extend(self._format_signals(field_path, current_python, current_json))
        score = min((signal.score for signal in signals), default=1.0)
        route = self._field_route(signals, score, thresholds)
        candidates = tuple(
            dict.fromkeys(
                [
                    *(evidence.candidate_values if evidence is not None else ()),
                    *(candidate for item in ocr for candidate in item.candidate_values),
                ]
            )
        )
        return FieldDecision(
            field_path=field_path,
            current_value=current_json,
            candidate_values=candidates,
            signals=tuple(signals),
            score=score,
            route=route,
            user_action=(
                self._user_action(signals) if route is not ValidationRoute.ACCEPTED else None
            ),
        )

    @staticmethod
    def _vision_signals(
        evidence: FieldEvidence,
        current_value: JsonValue,
    ) -> tuple[ValidationSignal, ...]:
        if evidence.readability is Readability.READABLE:
            readability_verdict = SignalVerdict.PASSED
            readability_score = 1.0
        elif evidence.readability is Readability.PARTIALLY_READABLE:
            readability_verdict = SignalVerdict.WARNING
            readability_score = 0.5
        else:
            readability_verdict = SignalVerdict.FAILED
            readability_score = 0.0
        readability_signal = ValidationSignal(
            field_path=evidence.field_path,
            rule="vision_readability",
            verdict=readability_verdict,
            score=readability_score,
            message=f"视觉可读性为 {evidence.readability.value}",
            observed_value=evidence.readability.value,
            expected=Readability.READABLE.value,
            failure_route=ValidationRoute.REVIEW_REQUIRED,
        )

        ambiguous = evidence.ambiguous or len(evidence.candidate_values) > 1
        candidates_present = bool(evidence.candidate_values) or current_value is None
        candidates_passed = not ambiguous and candidates_present
        candidate_signal = ValidationSignal(
            field_path=evidence.field_path,
            rule="vision_candidates",
            verdict=SignalVerdict.PASSED if candidates_passed else SignalVerdict.FAILED,
            score=1.0 if candidates_passed else 0.4,
            message=(
                "视觉候选值唯一且无歧义"
                if candidates_passed
                else "视觉候选值存在歧义或缺少直接证据"
            ),
            observed_value=list(evidence.candidate_values),
            expected="零或一个无歧义直接候选；缺失值由 field_presence 单独判断",
            failure_route=ValidationRoute.REVIEW_REQUIRED,
        )
        return readability_signal, candidate_signal

    @classmethod
    def _ocr_signal(
        cls,
        field_path: str | None,
        current_value: JsonValue,
        evidence: FieldEvidence | None,
        observations: tuple[OCRFieldObservation, ...],
        threshold: float,
    ) -> ValidationSignal:
        vision_values = list(evidence.candidate_values) if evidence is not None else []
        if current_value is not None:
            vision_values.append(cls._text(current_value))
        ocr_values = [candidate for item in observations for candidate in item.candidate_values]
        match_score = max(
            (
                SequenceMatcher(None, cls._normalize(left), cls._normalize(right)).ratio()
                for left in vision_values
                for right in ocr_values
                if cls._normalize(left) and cls._normalize(right)
            ),
            default=0.0,
        )
        passed = bool(vision_values and ocr_values) and match_score >= threshold
        return ValidationSignal(
            field_path=field_path,
            rule="ocr_vision_consistency",
            verdict=SignalVerdict.PASSED if passed else SignalVerdict.FAILED,
            score=match_score,
            message="OCR 与 Vision 结果一致" if passed else "OCR 与 Vision 结果不一致",
            observed_value=cast(
                JsonValue,
                {
                    "vision_candidates": vision_values,
                    "ocr_candidates": ocr_values,
                },
            ),
            expected=f"相似度 >= {threshold}",
            failure_route=ValidationRoute.REVIEW_REQUIRED,
        )

    @staticmethod
    def _ocr_comparison_signal(
        comparison: OCRVisionComparison,
    ) -> ValidationSignal | None:
        if comparison.outcome in {
            OCRComparisonOutcome.UNAVAILABLE,
            OCRComparisonOutcome.VISION_ONLY,
        } and not comparison.review_required:
            return None
        passed = comparison.outcome in {
            OCRComparisonOutcome.CORROBORATED,
            OCRComparisonOutcome.CONSISTENT,
        }
        return ValidationSignal(
            field_path=comparison.canonical_field_path,
            rule="ocr_multi_source_comparison",
            verdict=SignalVerdict.PASSED if passed else SignalVerdict.FAILED,
            score=1.0 if passed else 0.0,
            message=(
                "OCR 与 Vision 相互印证；该信号不代表业务真值"
                if passed
                else f"OCR 多源比对结果为 {comparison.outcome.value}"
            ),
            observed_value={
                "outcome": comparison.outcome.value,
                "vision_candidates": list(comparison.vision_candidates),
                "ocr_candidates": list(comparison.ocr_candidates),
                "supporting_sources": list(comparison.supporting_sources),
                "conflicting_sources": list(comparison.conflicting_sources),
                "reason_codes": list(comparison.reason_codes),
            },
            expected="corroborated；一致仅作为正向技术验证信号",
            failure_route=ValidationRoute.REVIEW_REQUIRED,
        )

    def _business_rule_signals(
        self,
        python_values: Mapping[str, object],
        json_values: Mapping[str, JsonValue],
    ) -> dict[str, tuple[ValidationSignal, ...]]:
        signals: dict[str, list[ValidationSignal]] = {}
        rules: list[
            tuple[
                str,
                tuple[str, ...],
                Callable[[tuple[Decimal, ...]], Decimal],
                str,
            ]
        ] = [
            (
                "business_rule.amount_total_consistency",
                ("invoice_total_amount", "invoice_total_tax_amount", "invoice_total_tax_price"),
                lambda values: values[0] + values[1] - values[2],
                "金额 + 税额 = 价税合计",
            ),
        ]
        for rule, fields, difference, expectation in rules:
            if not all(
                field in python_values and python_values[field] is not None
                for field in fields
            ):
                continue
            try:
                values = tuple(Decimal(str(python_values[field])) for field in fields)
                delta = abs(difference(values))
            except (InvalidOperation, TypeError, ValueError):
                delta = Decimal("Infinity")
            observed = cast(JsonValue, {field: json_values.get(field) for field in fields})
            for field in fields:
                tolerance = self._policy.resolve(field).amount_tolerance
                passed = delta <= tolerance
                signals.setdefault(field, []).append(
                    ValidationSignal(
                        field_path=field,
                        rule=rule,
                        verdict=SignalVerdict.PASSED if passed else SignalVerdict.FAILED,
                        score=1.0 if passed else 0.0,
                        message=f"{rule} 通过" if passed else f"{rule} 不一致",
                        observed_value=observed,
                        expected=f"{expectation}，误差 <= {tolerance}",
                        failure_route=ValidationRoute.REVIEW_REQUIRED,
                    )
                )
        return {field: tuple(items) for field, items in signals.items()}

    @staticmethod
    def _format_signals(
        field_path: str | None,
        python_value: object,
        json_value: JsonValue,
    ) -> tuple[ValidationSignal, ...]:
        if field_path is None or json_value is None:
            return ()
        leaf_name = field_path.rsplit(".", 1)[-1]
        if leaf_name in {"invoice_date", "bookkeeping_datetime"}:
            passed = isinstance(python_value, (date, datetime))
            return (
                EvidenceBasedExtractionValidator._signal(
                    field_path,
                    "format.date",
                    passed=passed,
                    failure_route=ValidationRoute.REVIEW_REQUIRED,
                    message="日期类型有效" if passed else "日期格式无效",
                    observed=json_value,
                    expected="ISO date/datetime compatible with InvoiceExtraction",
                ),
            )
        if leaf_name == "currency":
            passed = isinstance(python_value, str) and bool(
                _CURRENCY_PATTERN.fullmatch(python_value)
            )
            return (
                EvidenceBasedExtractionValidator._signal(
                    field_path,
                    "format.currency",
                    passed=passed,
                    failure_route=ValidationRoute.REVIEW_REQUIRED,
                    message="币种格式有效" if passed else "币种必须为 3 位大写字母",
                    observed=json_value,
                    expected="^[A-Z]{3}$",
                ),
            )
        if leaf_name in _IDENTIFIER_FIELDS:
            passed = isinstance(python_value, str) and bool(
                _IDENTIFIER_PATTERN.fullmatch(python_value)
            )
            return (
                EvidenceBasedExtractionValidator._signal(
                    field_path,
                    "format.identifier",
                    passed=passed,
                    failure_route=ValidationRoute.REVIEW_REQUIRED,
                    message="编号格式有效" if passed else "编号含不允许的字符",
                    observed=json_value,
                    expected="仅字母、数字、连字符或下划线",
                ),
            )
        return ()

    @staticmethod
    def _field_route(
        signals: list[ValidationSignal],
        score: float,
        thresholds: ValidationThresholds,
    ) -> ValidationRoute:
        if any(
            signal.verdict is not SignalVerdict.PASSED
            and signal.failure_route is ValidationRoute.REJECTED
            and signal.score <= thresholds.reject_threshold
            for signal in signals
        ):
            return ValidationRoute.REJECTED
        if score < thresholds.accept_threshold or any(
            signal.verdict is not SignalVerdict.PASSED for signal in signals
        ):
            return ValidationRoute.REVIEW_REQUIRED
        return ValidationRoute.ACCEPTED

    @staticmethod
    def _overall_route(decisions: tuple[FieldDecision, ...]) -> ValidationRoute:
        routes = {decision.route for decision in decisions}
        if ValidationRoute.REJECTED in routes:
            return ValidationRoute.REJECTED
        if ValidationRoute.REVIEW_REQUIRED in routes:
            return ValidationRoute.REVIEW_REQUIRED
        return ValidationRoute.ACCEPTED

    @staticmethod
    def _issues(decisions: tuple[FieldDecision, ...]) -> tuple[ValidationIssue, ...]:
        unique: dict[tuple[str, str, str | None], ValidationIssue] = {}
        for decision in decisions:
            if decision.route is ValidationRoute.ACCEPTED:
                continue
            for signal in decision.signals:
                if signal.verdict is SignalVerdict.PASSED:
                    continue
                issue = ValidationIssue(
                    code=signal.rule,
                    message=signal.message,
                    field_path=decision.field_path,
                )
                unique[(issue.code, issue.message, issue.field_path)] = issue
        return tuple(unique.values())

    @staticmethod
    def _user_action(signals: list[ValidationSignal]) -> str:
        rules = {signal.rule for signal in signals if signal.verdict is not SignalVerdict.PASSED}
        if rules & {"image_clarity", "image_resolution"}:
            return "请上传更清晰或更高分辨率的文档，或人工核对并提交该字段。"
        if "field_presence" in rules:
            return "请查看原始文档并填写该字段；文档未提供时请明确提交 null。"
        if any(rule.startswith("business_rule.") for rule in rules):
            return "请核对原始金额、税额及合计关系，并提交完整修正值。"
        if any(rule.startswith("format.") for rule in rules):
            return "请按原始文档核对字段格式并提交修正值。"
        if rules & {
            "ocr_multi_source_comparison",
            "ocr_vision_consistency",
            "vision_candidates",
            "vision_evidence_presence",
        }:
            return "请在候选值之间核对原始文档，并提交明确的最终值。"
        return "请核对原始文档并提交该字段的明确值和修正原因。"

    @staticmethod
    def _quality_for_evidence(
        evidence: FieldEvidence | None,
        quality: tuple[PageQuality, ...],
    ) -> PageQuality | None:
        if not quality:
            return None
        if evidence is not None and evidence.page_number is not None:
            for page in quality:
                if page.page_number == evidence.page_number:
                    return page
        return min(
            quality,
            key=lambda page: (page.clarity_score, page.width * page.height, page.page_number),
        )

    @staticmethod
    def _group_ocr(
        observations: tuple[OCRFieldObservation, ...],
    ) -> dict[str, tuple[OCRFieldObservation, ...]]:
        grouped: dict[str, list[OCRFieldObservation]] = {}
        for observation in observations:
            grouped.setdefault(observation.field_path, []).append(observation)
        return {path: tuple(items) for path, items in grouped.items()}

    @staticmethod
    def _group_ocr_comparisons(
        comparisons: tuple[OCRVisionComparison, ...],
    ) -> dict[str, OCRVisionComparison]:
        grouped: dict[str, OCRVisionComparison] = {}
        for comparison in sorted(
            comparisons,
            key=lambda item: (
                item.canonical_field_path,
                item.outcome.value,
                item.reason_codes,
            ),
        ):
            grouped.setdefault(comparison.canonical_field_path, comparison)
        return grouped

    @staticmethod
    def _group_anomalies(
        anomalies: tuple[ExtractionAnomaly, ...],
    ) -> dict[str | None, tuple[ExtractionAnomaly, ...]]:
        grouped: dict[str | None, list[ExtractionAnomaly]] = {}
        for anomaly in anomalies:
            grouped.setdefault(anomaly.field_path, []).append(anomaly)
        return {path: tuple(items) for path, items in grouped.items()}

    @staticmethod
    def _signal(
        field_path: str | None,
        rule: str,
        *,
        passed: bool,
        failure_route: ValidationRoute,
        message: str,
        observed: JsonValue,
        expected: str,
    ) -> ValidationSignal:
        return ValidationSignal(
            field_path=field_path,
            rule=rule,
            verdict=SignalVerdict.PASSED if passed else SignalVerdict.FAILED,
            score=1.0 if passed else 0.0,
            message=message,
            observed_value=observed,
            expected=expected,
            failure_route=failure_route,
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(character.casefold() for character in value if character.isalnum())

    @staticmethod
    def _text(value: JsonValue) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
