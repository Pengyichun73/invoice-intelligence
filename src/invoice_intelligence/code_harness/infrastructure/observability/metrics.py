"""Low-cardinality Harness metrics adapter."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from time import monotonic

from ...application.ports.harness_observability import (
    HarnessObservability,
    validate_stage,
)
from invoice_intelligence.infrastructure.observability.metrics import (
    MetricsRegistry,
    get_metrics_registry,
)


class MetricsHarnessObservability(HarnessObservability):
    """Emit only bounded stage and outcome labels to the existing registry."""

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        self._registry = registry or get_metrics_registry()

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
        del trace_id, tenant_id
        validate_stage(stage)
        if not operation or operation != operation.strip():
            raise ValueError("Harness observability operation must be normalized")
        labels = {
            "stage": stage.removeprefix("code_harness.")[:64],
            "operation": operation[:64],
        }
        metric_attributes: dict[str, str | int | float | bool | None] = dict(attributes or {})
        if metric_attributes:
            attempt_count = metric_attributes.get("attempt_count")
            if isinstance(attempt_count, int) and attempt_count >= 0:
                labels["attempt"] = str(min(attempt_count, 1000))
            for key in (
                "schema_version",
                "parser_version",
                "grammar_version",
                "index_version",
                "model_version",
                "prompt_version",
                "patch_policy_version",
                "sandbox_policy_version",
                "watchdog_version",
            ):
                value = metric_attributes.get(key)
                if isinstance(value, str) and value.strip():
                    labels[key] = value[:64]
        started = monotonic()
        self._registry.increment("code_harness_stage_total", labels=labels)
        try:
            yield
        except BaseException:
            self._registry.increment(
                "code_harness_stage_failures_total",
                labels=labels,
            )
            raise
        finally:
            self._registry.set_gauge(
                "code_harness_stage_duration_seconds",
                max(0.0, monotonic() - started),
                labels=labels,
            )
            for token_name in ("input_tokens", "output_tokens", "total_tokens"):
                token_count = metric_attributes.get(token_name)
                if isinstance(token_count, int) and token_count >= 0:
                    self._registry.increment(
                        f"code_harness_{token_name}_total",
                        value=min(token_count, 10_000_000),
                        labels=labels,
                    )
