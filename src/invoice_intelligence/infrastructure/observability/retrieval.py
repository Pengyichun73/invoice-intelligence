"""Context-local remote usage aggregation for retrieval traces."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from invoice_intelligence.application.ports.governance import (
    RemoteCallAggregate,
    RemoteCallObservation,
)


@dataclass(slots=True)
class _MutableAggregate:
    error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    has_token_usage: bool = False
    estimated_cost: float = 0.0
    has_cost: bool = False


class ContextVarRetrievalTelemetry:
    """Keep concurrent request usage isolated without exposing SDK payloads."""

    def __init__(self) -> None:
        self._current_trace: ContextVar[str | None] = ContextVar(
            "invoice_retrieval_trace_id",
            default=None,
        )
        self._aggregates: dict[str, _MutableAggregate] = {}

    @contextmanager
    def bind(self, trace_id: str, tenant_id: str) -> Iterator[None]:
        if not trace_id.strip() or not tenant_id.strip():
            raise ValueError("Trace and tenant identifiers must not be empty")
        self._aggregates.setdefault(trace_id, _MutableAggregate())
        token = self._current_trace.set(trace_id)
        try:
            yield
        finally:
            self._current_trace.reset(token)

    def record_remote_call(self, observation: RemoteCallObservation) -> None:
        trace_id = self._current_trace.get()
        if trace_id is None:
            return
        aggregate = self._aggregates.setdefault(trace_id, _MutableAggregate())
        if observation.outcome != "success":
            aggregate.error_count += 1
        if observation.input_tokens is not None:
            aggregate.input_tokens += observation.input_tokens
            aggregate.has_token_usage = True
        if observation.output_tokens is not None:
            aggregate.output_tokens += observation.output_tokens
            aggregate.has_token_usage = True
        if observation.estimated_cost is not None:
            aggregate.estimated_cost += observation.estimated_cost
            aggregate.has_cost = True

    def snapshot(self, trace_id: str) -> RemoteCallAggregate:
        aggregate = self._aggregates.get(trace_id, _MutableAggregate())
        return RemoteCallAggregate(
            error_count=aggregate.error_count,
            input_tokens=aggregate.input_tokens if aggregate.has_token_usage else None,
            output_tokens=aggregate.output_tokens if aggregate.has_token_usage else None,
            estimated_cost=aggregate.estimated_cost if aggregate.has_cost else None,
        )

    def clear(self, trace_id: str) -> None:
        self._aggregates.pop(trace_id, None)
