"""Trusted tenant and actor boundary for Harness application services."""

from dataclasses import dataclass

from .errors import require_text


@dataclass(frozen=True, slots=True)
class HarnessTenantContext:
    tenant_id: str
    actor_id: str
    trace_id: str | None = None

    def __post_init__(self) -> None:
        require_text("tenant_id", self.tenant_id)
        require_text("actor_id", self.actor_id)
        if self.trace_id is not None:
            require_text("trace_id", self.trace_id, max_length=128)

    def assert_tenant(self, tenant_id: str) -> None:
        if tenant_id != self.tenant_id:
            raise ValueError("tenant scope mismatch")

