"""Auditable postmortem facts and admission decisions."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .errors import require_sha256, require_text


class AdmissionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    SUSPENDED = "suspended"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class PostmortemSourceEvent:
    event_id: str
    tenant_id: str
    fingerprint: str
    version_scope: str
    source_type: str
    payload_summary: str
    payload_checksum_sha256: str
    created_at: datetime
    error_signature: str | None = None
    root_cause: str | None = None
    solution_pattern: str | None = None
    affected_language: str | None = None
    affected_symbol_kind: str | None = None
    patch_shape: str | None = None
    source_trace_id: str | None = None
    source_repository_id: str | None = None
    source_snapshot_id: str | None = None
    source_revision: str | None = None
    source_task_id: str | None = None
    source_patch_id: str | None = None
    source_execution_id: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "tenant_id",
            "fingerprint",
            "version_scope",
            "source_type",
            "payload_summary",
        ):
            require_text(name, getattr(self, name), max_length=512)
        require_sha256("payload_checksum_sha256", self.payload_checksum_sha256)
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        for name in (
            "error_signature",
            "root_cause",
            "solution_pattern",
            "affected_language",
            "affected_symbol_kind",
            "patch_shape",
            "source_trace_id",
            "source_repository_id",
            "source_snapshot_id",
            "source_revision",
            "source_task_id",
            "source_patch_id",
            "source_execution_id",
        ):
            value = getattr(self, name)
            if value is not None:
                require_text(name, value, max_length=4096)


@dataclass(frozen=True, slots=True)
class Postmortem:
    postmortem_id: str
    tenant_id: str
    fingerprint: str
    version_scope: str
    occurrence_count: int
    admission_status: AdmissionStatus
    source_event_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    error_signature: str | None = None
    root_cause: str | None = None
    solution_pattern: str | None = None
    affected_language: str | None = None
    affected_symbol_kind: str | None = None
    patch_shape: str | None = None
    source_trace_id: str | None = None
    revision: int = 1

    def __post_init__(self) -> None:
        for name in ("postmortem_id", "tenant_id", "fingerprint", "version_scope"):
            require_text(name, getattr(self, name), max_length=512)
        if self.occurrence_count < 1 or not self.source_event_ids:
            raise ValueError("postmortem requires at least one source event")
        if len(self.source_event_ids) != len(set(self.source_event_ids)):
            raise ValueError("postmortem source events must be unique")
        for value in (self.created_at, self.updated_at):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("postmortem timestamps must be timezone-aware")
        if self.revision < 1:
            raise ValueError("postmortem revision must be positive")
        for name in (
            "error_signature",
            "root_cause",
            "solution_pattern",
            "affected_language",
            "affected_symbol_kind",
            "patch_shape",
            "source_trace_id",
        ):
            value = getattr(self, name)
            if value is not None:
                require_text(name, value, max_length=4096)
