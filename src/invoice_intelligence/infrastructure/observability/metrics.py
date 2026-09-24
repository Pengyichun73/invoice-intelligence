"""低基数、脱敏的进程级运行指标。"""

from __future__ import annotations

from collections import defaultdict
from threading import Lock


class MetricsRegistry:
    """Prometheus text exposition without external runtime dependency."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = defaultdict(int)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def increment(self, name: str, *, value: int = 1, labels: dict[str, str] | None = None) -> None:
        if value < 0:
            raise ValueError("counter increment must be non-negative")
        key = (self._normalize_name(name), self._normalize_labels(labels))
        with self._lock:
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
        key = (self._normalize_name(name), self._normalize_labels(labels))
        with self._lock:
            self._gauges[key] = float(value)

    def record_provider(self, *, provider: str, operation: str, outcome: str) -> None:
        self.increment(
            "invoice_provider_calls_total",
            labels={"provider": provider, "operation": operation, "outcome": outcome},
        )
        if outcome == "circuit_open":
            self.increment(
                "invoice_provider_circuit_open_total",
                labels={"provider": provider, "operation": operation},
            )

    def record_worker(
        self,
        *,
        worker: str,
        outcome: str,
        retryable: bool = False,
        lease_expired: bool = False,
        backlog: int | None = None,
        oldest_age_seconds: float | None = None,
    ) -> None:
        labels = {"worker": worker, "outcome": outcome}
        self.increment("invoice_worker_processed_total", labels=labels)
        if retryable:
            self.increment("invoice_worker_retries_total", labels={"worker": worker})
        if lease_expired:
            self.increment("invoice_worker_lease_expired_total", labels={"worker": worker})
        if backlog is not None:
            self.set_gauge(
                "invoice_worker_backlog",
                backlog,
                labels={"worker": worker},
            )
        if oldest_age_seconds is not None:
            self.set_gauge(
                "invoice_worker_oldest_task_age_seconds",
                oldest_age_seconds,
                labels={"worker": worker},
            )

    def record_audit_failure(self, *, sink: str, error_code: str) -> None:
        self.increment(
            "invoice_audit_write_failures_total",
            labels={"sink": sink, "error_code": error_code},
        )

    def render(self) -> str:
        with self._lock:
            counters = tuple(self._counters.items())
            gauges = tuple(self._gauges.items())
        lines: list[str] = []
        for (name, labels), value in counters:
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{self._render_labels(labels)} {value}")
        for (name, labels), value in gauges:
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{self._render_labels(labels)} {value}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = name.strip()
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
        if not normalized or any(char not in allowed for char in normalized):
            raise ValueError("metric name must be normalized")
        return normalized

    @staticmethod
    def _normalize_labels(labels: dict[str, str] | None) -> tuple[tuple[str, str], ...]:
        result = []
        for key, value in sorted((labels or {}).items()):
            if not key or not value or any(char in key + value for char in "\r\n"):
                raise ValueError("metric labels must be non-empty and single-line")
            result.append((key, value[:128]))
        return tuple(result)

    @staticmethod
    def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        escaped = ",".join(
            f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
            for key, value in labels
        )
        return "{" + escaped + "}"


_REGISTRY = MetricsRegistry()


def get_metrics_registry() -> MetricsRegistry:
    return _REGISTRY
