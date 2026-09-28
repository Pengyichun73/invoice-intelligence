"""Structured, sensitive-value-free OCR metric logging and persistence."""

import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, Integer, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from invoice_intelligence.application.ports.observability import (
    OCRComparisonMetric,
    OCRMetricsSummary,
    OCRProviderCallMetric,
    OCRProviderVersionSummary,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    OCRComparisonMetricEventRow,
    OCRPageMetricEventRow,
    OCRProviderMetricEventRow,
)

_LOGGER = logging.getLogger(__name__)


class StructuredLoggingOCRTelemetry:
    """Emit aggregation-ready counters and timings through structured logs."""

    def record_provider_call(self, metric: OCRProviderCallMetric) -> None:
        outcomes = tuple(page.outcome for page in metric.pages)
        success_count = outcomes.count("success")
        timeout_count = outcomes.count("timeout")
        circuit_open_count = outcomes.count("circuit_open")
        schema_error_count = outcomes.count("schema_error")
        successful_batch = metric.page_count > 0 and success_count == metric.page_count
        text_box_count = sum(page.text_box_count for page in metric.pages)
        _LOGGER.info(
            "ocr_provider_metrics",
            extra={
                "trace_id": metric.trace_id,
                "provider_name": metric.provider_name,
                "provider_version": metric.provider_version,
                "model_version": metric.model_version,
                "config_version": metric.config_version,
                "ocr_call_count": 1,
                "ocr_total_latency_ms": metric.latency_ms,
                "page_count": metric.page_count,
                "ocr_success_count": success_count,
                "ocr_timeout_count": timeout_count,
                "ocr_circuit_open_count": circuit_open_count,
                "ocr_schema_error_count": schema_error_count,
                "ocr_other_error_count": sum(
                    outcome not in {"success", "timeout", "circuit_open", "schema_error"}
                    for outcome in outcomes
                )
                + int(metric.error_type is not None and not outcomes),
                "text_box_count": text_box_count,
                "empty_ocr_rate_numerator": int(successful_batch and text_box_count == 0),
                "empty_ocr_rate_denominator": int(successful_batch),
                "error_type": metric.error_type,
            },
        )
        for page in metric.pages:
            _LOGGER.info(
                "ocr_page_metrics",
                extra={
                    "trace_id": metric.trace_id,
                    "provider_name": metric.provider_name,
                    "provider_version": metric.provider_version,
                    "model_version": metric.model_version,
                    "config_version": metric.config_version,
                    "page_number": page.page_number,
                    "ocr_page_latency_ms": page.latency_ms,
                    "status_code": page.status_code,
                    "outcome": page.outcome,
                    "text_box_count": page.text_box_count,
                },
            )

    def record_comparison(self, metric: OCRComparisonMetric) -> None:
        _LOGGER.info(
            "ocr_comparison_metrics",
            extra={
                "trace_ids": metric.trace_ids,
                "raw_observation_count": metric.raw_observation_count,
                "bound_field_count": metric.bound_field_count,
                "corroborated_count": metric.corroborated_count,
                "conflicting_count": metric.conflicting_count,
                "ocr_only_count": metric.ocr_only_count,
                "vision_only_count": metric.vision_only_count,
                "unresolved_count": metric.unresolved_count,
                "unavailable_count": metric.unavailable_count,
                "ocr_conflict_review_rate_numerator": int(
                    metric.conflict_review_required
                ),
                "ocr_conflict_review_rate_denominator": 1,
            },
        )


class SQLAlchemyOCRTelemetry(StructuredLoggingOCRTelemetry):
    """Persist bounded OCR counters while retaining the structured log stream."""

    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def record_provider_call(self, metric: OCRProviderCallMetric) -> None:
        super().record_provider_call(metric)
        outcomes = tuple(page.outcome for page in metric.pages)
        success_count = outcomes.count("success")
        successful_batch = metric.page_count > 0 and success_count == metric.page_count
        event_id = uuid4().hex
        try:
            with self._sessions.begin() as session:
                session.add(
                    OCRProviderMetricEventRow(
                        event_id=event_id,
                        trace_id=metric.trace_id,
                        provider_name=metric.provider_name,
                        provider_version=metric.provider_version,
                        model_version=metric.model_version,
                        config_version=metric.config_version,
                        page_count=metric.page_count,
                        latency_ms=metric.latency_ms,
                        success_count=success_count,
                        timeout_count=outcomes.count("timeout"),
                        circuit_open_count=outcomes.count("circuit_open"),
                        schema_error_count=outcomes.count("schema_error"),
                        other_error_count=sum(
                            outcome
                            not in {"success", "timeout", "circuit_open", "schema_error"}
                            for outcome in outcomes
                        )
                        + int(metric.error_type is not None and not outcomes),
                        text_box_count=sum(page.text_box_count for page in metric.pages),
                        empty_rate_numerator=int(
                            successful_batch
                            and sum(page.text_box_count for page in metric.pages) == 0
                        ),
                        empty_rate_denominator=int(successful_batch),
                        created_at=datetime.now(UTC),
                    )
                )
                session.add_all(
                    OCRPageMetricEventRow(
                        event_id=uuid4().hex,
                        provider_event_id=event_id,
                        page_number=page.page_number,
                        latency_ms=page.latency_ms if page.latency_ms is not None else 0.0,
                        status_code=page.status_code,
                        outcome=page.outcome,
                        text_box_count=page.text_box_count,
                    )
                    for page in metric.pages
                )
        except SQLAlchemyError as exc:
            _LOGGER.warning(
                "ocr_metric_persistence_failed",
                extra={"error_type": type(exc).__name__},
            )

    def record_comparison(self, metric: OCRComparisonMetric) -> None:
        super().record_comparison(metric)
        try:
            with self._sessions.begin() as session:
                session.add(
                    OCRComparisonMetricEventRow(
                        event_id=uuid4().hex,
                        trace_ids_json=list(metric.trace_ids),
                        raw_observation_count=metric.raw_observation_count,
                        bound_field_count=metric.bound_field_count,
                        corroborated_count=metric.corroborated_count,
                        conflicting_count=metric.conflicting_count,
                        ocr_only_count=metric.ocr_only_count,
                        vision_only_count=metric.vision_only_count,
                        unresolved_count=metric.unresolved_count,
                        unavailable_count=metric.unavailable_count,
                        conflict_review_required=metric.conflict_review_required,
                        created_at=datetime.now(UTC),
                    )
                )
        except SQLAlchemyError as exc:
            _LOGGER.warning(
                "ocr_metric_persistence_failed",
                extra={"error_type": type(exc).__name__},
            )

    async def summarize(self) -> OCRMetricsSummary:
        return await asyncio.to_thread(self._summarize_sync)

    def _summarize_sync(self) -> OCRMetricsSummary:
        with self._sessions() as session:
            provider = session.execute(
                select(
                    func.count(OCRProviderMetricEventRow.event_id),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.page_count), 0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.latency_ms), 0.0),
                    func.coalesce(func.avg(OCRProviderMetricEventRow.latency_ms), 0.0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.success_count), 0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.timeout_count), 0),
                    func.coalesce(
                        func.sum(OCRProviderMetricEventRow.circuit_open_count), 0
                    ),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.schema_error_count), 0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.other_error_count), 0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.text_box_count), 0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.empty_rate_numerator), 0),
                    func.coalesce(func.sum(OCRProviderMetricEventRow.empty_rate_denominator), 0),
                )
            ).one()
            average_page_latency = session.scalar(
                select(
                    func.coalesce(
                        func.avg(func.nullif(OCRPageMetricEventRow.latency_ms, 0.0)),
                        0.0,
                    )
                )
            )
            comparison = session.execute(
                select(
                    func.count(OCRComparisonMetricEventRow.event_id),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.bound_field_count), 0),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.corroborated_count), 0),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.conflicting_count), 0),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.ocr_only_count), 0),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.vision_only_count), 0),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.unresolved_count), 0),
                    func.coalesce(func.sum(OCRComparisonMetricEventRow.unavailable_count), 0),
                    func.coalesce(
                        func.sum(
                            func.cast(
                                OCRComparisonMetricEventRow.conflict_review_required,
                                Integer,
                            )
                        ),
                        0,
                    ),
                )
            ).one()
            versions = session.execute(
                select(
                    OCRProviderMetricEventRow.provider_name,
                    OCRProviderMetricEventRow.provider_version,
                    OCRProviderMetricEventRow.model_version,
                    OCRProviderMetricEventRow.config_version,
                    func.count(OCRProviderMetricEventRow.event_id),
                )
                .group_by(
                    OCRProviderMetricEventRow.provider_name,
                    OCRProviderMetricEventRow.provider_version,
                    OCRProviderMetricEventRow.model_version,
                    OCRProviderMetricEventRow.config_version,
                )
                .order_by(
                    OCRProviderMetricEventRow.provider_name,
                    OCRProviderMetricEventRow.provider_version,
                    OCRProviderMetricEventRow.model_version,
                    OCRProviderMetricEventRow.config_version,
                )
            ).all()
        empty_denominator = int(provider[11])
        comparison_count = int(comparison[0])
        return OCRMetricsSummary(
            provider_call_count=int(provider[0]),
            page_call_count=int(provider[1]),
            total_latency_ms=float(provider[2]),
            average_call_latency_ms=float(provider[3]),
            average_page_latency_ms=float(average_page_latency or 0.0),
            success_count=int(provider[4]),
            timeout_count=int(provider[5]),
            circuit_open_count=int(provider[6]),
            schema_error_count=int(provider[7]),
            other_error_count=int(provider[8]),
            text_box_count=int(provider[9]),
            bound_field_count=int(comparison[1]),
            corroborated_count=int(comparison[2]),
            conflicting_count=int(comparison[3]),
            ocr_only_count=int(comparison[4]),
            vision_only_count=int(comparison[5]),
            unresolved_count=int(comparison[6]),
            unavailable_count=int(comparison[7]),
            empty_ocr_rate=(
                int(provider[10]) / empty_denominator if empty_denominator else None
            ),
            conflict_review_required_rate=(
                int(comparison[8]) / comparison_count if comparison_count else None
            ),
            providers=tuple(
                OCRProviderVersionSummary(
                    provider_name=str(row[0]),
                    provider_version=str(row[1]),
                    model_version=str(row[2]),
                    config_version=str(row[3]),
                    call_count=int(row[4]),
                )
                for row in versions
            ),
        )
