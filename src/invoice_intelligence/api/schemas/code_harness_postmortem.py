"""HTTP contracts for bounded Harness postmortem governance."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from invoice_intelligence.code_harness.domain.postmortem import AdmissionStatus


class PostmortemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    postmortem_id: str
    fingerprint: str
    version_scope: str
    occurrence_count: int
    admission_status: AdmissionStatus
    source_event_count: int
    created_at: datetime
    updated_at: datetime
    error_signature: str | None
    root_cause: str | None
    solution_pattern: str | None
    affected_language: str | None
    affected_symbol_kind: str | None
    patch_shape: str | None
    source_trace_id: str | None
    revision: int


class PostmortemAdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_revision: int = Field(gt=0)
    status: AdmissionStatus
    reason_code: str = Field(min_length=1, max_length=128)
