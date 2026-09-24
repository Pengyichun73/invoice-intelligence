"""Authentication and security-audit boundaries independent of HTTP and JWT SDKs."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class AuthContext:
    subject: str
    tenant_id: str
    reviewer_id: str
    roles: frozenset[str]
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        for name, value, maximum in (
            ("subject", self.subject, 256),
            ("tenant_id", self.tenant_id, 128),
            ("reviewer_id", self.reviewer_id, 128),
        ):
            if not value or value != value.strip() or len(value) > maximum:
                raise ValueError(f"AuthContext {name} must be bounded and normalized")
        for name, values in (("roles", self.roles), ("scopes", self.scopes)):
            if len(values) > 256 or any(
                not value or value != value.strip() or len(value) > 128
                for value in values
            ):
                raise ValueError(f"AuthContext {name} must be bounded and normalized")


class AuthContextProvider(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthContext:
        """Validate one bearer token and return trusted claims."""

        ...


@dataclass(frozen=True, slots=True)
class SecurityAuditEvent:
    event_id: str
    event_type: str
    outcome: str
    reason_code: str
    trace_id: str
    request_path: str
    occurred_at: datetime
    subject: str | None = None
    tenant_id: str | None = None
    required_permission: str | None = None


class SecurityAuditSink(Protocol):
    async def record(self, event: SecurityAuditEvent) -> None:
        """Persist a metadata-only security event."""

        ...
