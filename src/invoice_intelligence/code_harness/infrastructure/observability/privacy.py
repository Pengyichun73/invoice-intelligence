"""Adapter from the shared privacy telemetry Port to Harness spans."""

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager

from ...application.ports.harness_observability import (
    HarnessObservability,
    validate_stage,
)
from invoice_intelligence.application.ports.observability import (
    PrivacyTelemetry,
    TraceStage,
)
from .metrics import MetricsHarnessObservability


class PrivacyTelemetryHarnessObservability(HarnessObservability):
    """Reuse the application's content-free trace implementation for Harness."""

    def __init__(
        self,
        telemetry: PrivacyTelemetry,
        metrics: MetricsHarnessObservability | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._metrics = metrics or MetricsHarnessObservability()

    @contextmanager
    def span(
        self,
        *,
        trace_id: str | None,
        tenant_id: str,
        stage: str,
        operation: str,
        attributes: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> Iterator[None]:
        validate_stage(stage)
        with ExitStack() as stack:
            if trace_id is not None:
                stack.enter_context(
                    self._telemetry.span(
                        trace_id=trace_id,
                        tenant_id=tenant_id,
                        stage=TraceStage.CODE_HARNESS,
                        operation=f"{stage}.{operation}"[:128],
                        attributes=_shared_safe_attributes(attributes),
                    )
                )
            stack.enter_context(
                self._metrics.span(
                    trace_id=trace_id,
                    tenant_id=tenant_id,
                    stage=stage,
                    operation=operation,
                    attributes=attributes,
                )
            )
            yield


def _shared_safe_attributes(
    attributes: Mapping[str, str | int | float | bool | None] | None,
) -> dict[str, str | int | float | bool | None]:
    allowed = {
        "attempt_count",
        "index_version",
        "run_id",
        "resource_id",
        "resource_type",
        "worker_id",
    }
    return {key: value for key, value in (attributes or {}).items() if key in allowed}
