"""PostgreSQL-backed security audit sink."""

import asyncio

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.ports.auth import SecurityAuditEvent
from invoice_intelligence.infrastructure.observability.metrics import (
    get_metrics_registry,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    SecurityAuditEventRow,
)


class SQLAlchemySecurityAuditSink:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = sessionmaker(engine, expire_on_commit=False, class_=Session)

    async def record(self, event: SecurityAuditEvent) -> None:
        try:
            await asyncio.to_thread(self._record_sync, event)
        except Exception:
            get_metrics_registry().record_audit_failure(
                sink="security_audit",
                error_code="audit_write_failed",
            )
            raise

    def _record_sync(self, event: SecurityAuditEvent) -> None:
        with self._session_factory.begin() as session:
            session.add(
                SecurityAuditEventRow(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    outcome=event.outcome,
                    reason_code=event.reason_code,
                    subject=event.subject,
                    tenant_id=event.tenant_id,
                    required_permission=event.required_permission,
                    trace_id=event.trace_id,
                    request_path=event.request_path,
                    occurred_at=event.occurred_at,
                )
            )
