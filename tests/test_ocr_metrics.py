import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from invoice_intelligence.application.ports.observability import (
    OCRComparisonMetric,
    OCRPageMetric,
    OCRProviderCallMetric,
)
from invoice_intelligence.infrastructure.observability.ocr import SQLAlchemyOCRTelemetry
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    Base,
    OCRComparisonMetricEventRow,
    OCRPageMetricEventRow,
    OCRProviderMetricEventRow,
)


@pytest.mark.asyncio
async def test_ocr_metrics_are_persisted_and_aggregated_without_values() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            OCRProviderMetricEventRow.__table__,
            OCRPageMetricEventRow.__table__,
            OCRComparisonMetricEventRow.__table__,
        ],
    )
    telemetry = SQLAlchemyOCRTelemetry(engine)
    telemetry.record_provider_call(
        OCRProviderCallMetric(
            trace_id="trace-1",
            provider_name="paddlex_ocr_http",
            provider_version="3.7.2",
            model_version="small-v6",
            config_version="config-v1",
            page_count=1,
            latency_ms=12.0,
            pages=(OCRPageMetric(1, 10.0, 200, "success", 3),),
        )
    )
    telemetry.record_comparison(
        OCRComparisonMetric(
            trace_ids=("trace-1",),
            raw_observation_count=3,
            bound_field_count=2,
            corroborated_count=1,
            conflicting_count=1,
            ocr_only_count=0,
            vision_only_count=0,
            unresolved_count=0,
            unavailable_count=0,
            conflict_review_required=True,
        )
    )

    summary = await telemetry.summarize()
    assert summary.provider_call_count == 1
    assert summary.page_call_count == 1
    assert summary.text_box_count == 3
    assert summary.corroborated_count == 1
    assert summary.conflicting_count == 1
    assert summary.conflict_review_required_rate == 1.0
    assert summary.providers[0].config_version == "config-v1"
