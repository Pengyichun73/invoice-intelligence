"""SQLAlchemy 2.x declarative models for business persistence only."""

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Business metadata root; checkpoint tables are deliberately absent."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


class TransactionCandidateRow(Base):
    __tablename__ = "transaction_candidates"
    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", name="uq_transaction_candidate_run"),
        Index("ix_transaction_candidate_fingerprint", "tenant_id", "fingerprint"),
    )


class TransactionAnalysisRow(Base):
    __tablename__ = "transaction_analyses"
    analysis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("transaction_candidates.candidate_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    classification_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    duplicate_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    suspicious_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    assessment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "candidate_id", name="uq_transaction_analysis_candidate"),
    )


class TransactionAnalysisAuditRow(Base):
    __tablename__ = "transaction_analysis_audits"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    assessment_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key_hash", name="uq_transaction_audit_idempotency"
        ),
    )


class DocumentRow(Base):
    __tablename__ = "documents"

    document_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    original_object_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("stored_objects.object_id", ondelete="RESTRICT"), nullable=True
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    storage_status: Mapped[str] = mapped_column(String(32), nullable=False, default="available")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StoredObjectRow(Base):
    __tablename__ = "stored_objects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "object_kind", "stable_key", name="uq_stored_object_key"),
        CheckConstraint("revision > 0", name="stored_objects_revision_positive"),
        CheckConstraint("attempt_count >= 0", name="stored_objects_attempt_nonnegative"),
    )

    object_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    parent_document_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=True
    )
    object_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    stable_key: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delete_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExtractionRunRow(Base):
    __tablename__ = "extraction_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    validation_route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExtractionResultRow(Base):
    __tablename__ = "extraction_results"

    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReviewTaskRow(Base):
    __tablename__ = "review_tasks"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    assigned_reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending_review', 'claimed', 'submitted', 'expired', 'cancelled')",
            name="ck_review_tasks_status_v2",
        ),
        CheckConstraint("priority BETWEEN 0 AND 100", name="ck_review_tasks_priority"),
        CheckConstraint("revision >= 1", name="ck_review_tasks_revision"),
        CheckConstraint(
            "(status = 'claimed' AND assigned_reviewer_id IS NOT NULL "
            "AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_review_tasks_lease_v2",
        ),
        Index(
            "ix_review_tasks_tenant_queue",
            "tenant_id",
            "status",
            "priority",
            "created_at",
            "review_id",
        ),
        Index(
            "ix_review_tasks_tenant_reviewer",
            "tenant_id",
            "assigned_reviewer_id",
            "status",
        ),
    )


class ReviewTaskAuditRow(Base):
    __tablename__ = "review_task_audits"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    review_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("review_tasks.review_id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    target_reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "review_id", "revision", name="uq_review_task_audits_revision"
        ),
        Index(
            "ix_review_task_audits_tenant_task_time",
            "tenant_id",
            "review_id",
            "created_at",
        ),
    )


class HumanCorrectionRow(Base):
    __tablename__ = "human_corrections"

    correction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    correction_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CorrectionEventRow(Base):
    __tablename__ = "correction_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    model_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    corrected_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    correction_reason: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_features_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    template_features_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    document_reference: Mapped[str] = mapped_column(Text, nullable=False)
    image_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_correction_events_scope_valid_created",
            "document_type",
            "field_path",
            "schema_version",
            "is_reviewed",
            "is_valid",
            "created_at",
        ),
    )


class MemoryReviewRecoveryRow(Base):
    __tablename__ = "memory_review_recoveries"

    recovery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=False,
    )
    correction_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("human_corrections.correction_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    correction_event_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    original_result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reviewed_result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    example_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'failed_retryable', 'completed')",
            name="ck_memory_review_recoveries_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_memory_review_recoveries_attempt_count",
        ),
        CheckConstraint(
            "(worker_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_memory_review_recoveries_lease",
        ),
        CheckConstraint(
            "(last_error_code IS NULL AND last_error_at IS NULL) "
            "OR (last_error_code IS NOT NULL AND last_error_at IS NOT NULL)",
            name="ck_memory_review_recoveries_last_error",
        ),
        Index(
            "ix_memory_review_recoveries_worker_queue",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "recovery_id",
        ),
        Index(
            "ix_memory_review_recoveries_tenant_run",
            "tenant_id",
            "run_id",
            "created_at",
        ),
    )


class CorrectionMemoryRow(Base):
    __tablename__ = "correction_memories"

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    event_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    disabled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "ix_correction_memories_scope_active",
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
            "is_reviewed",
            "is_valid",
        ),
        Index(
            "ix_correction_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class CorrectionMemorySourceRow(Base):
    __tablename__ = "correction_memory_sources"

    source_event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("correction_events.event_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    memory_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("correction_memories.memory_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelVersionRow(Base):
    __tablename__ = "model_versions"

    model_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_model_versions_tenant_version",
        ),
    )


class PromptVersionRow(Base):
    __tablename__ = "prompt_versions"

    prompt_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_prompt_versions_tenant_version",
        ),
    )


class IndexVersionRow(Base):
    __tablename__ = "index_versions"

    index_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dense_model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sparse_model_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    rerank_model_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    prompt_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_versions.prompt_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_index_versions_tenant_version",
        ),
        Index(
            "uq_index_versions_tenant_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )


class ExampleFeedbackRow(Base):
    __tablename__ = "example_feedback"

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    replay_key: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=False,
    )
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    label_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("correction_events.event_id", ondelete="RESTRICT"),
        nullable=True,
    )
    evidence_reference_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    prompt_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_versions.prompt_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "replay_key",
            name="uq_example_feedback_tenant_replay",
        ),
        CheckConstraint(
            "label_type IN ('confirmed_correct', 'corrected', 'confirmed_incorrect')",
            name="ck_example_feedback_label_type",
        ),
        CheckConstraint(
            "(label_type = 'corrected' AND source_event_id IS NOT NULL) OR "
            "(label_type IN ('confirmed_correct', 'confirmed_incorrect') "
            "AND source_event_id IS NULL)",
            name="ck_example_feedback_review_source",
        ),
        CheckConstraint(
            "label_type NOT IN ('corrected', 'confirmed_incorrect') OR reason IS NOT NULL",
            name="ck_example_feedback_review_reason",
        ),
        Index(
            "ix_example_feedback_tenant_fingerprint",
            "tenant_id",
            "semantic_fingerprint",
        ),
    )


class ReviewedExampleRow(Base):
    __tablename__ = "reviewed_examples"

    example_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    replay_key: Mapped[str] = mapped_column(String(64), nullable=False)
    semantic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_feedback_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("example_feedback.feedback_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_event_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("correction_events.event_id", ondelete="RESTRICT"),
        nullable=True,
    )
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.document_id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("extraction_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    prompt_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("prompt_versions.prompt_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    label_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    reviewed_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    template_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_reference_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    is_reviewed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "replay_key",
            name="uq_reviewed_examples_tenant_replay",
        ),
        UniqueConstraint(
            "tenant_id",
            "semantic_fingerprint",
            name="uq_reviewed_examples_tenant_fingerprint",
        ),
        UniqueConstraint(
            "source_event_id",
            name="uq_reviewed_examples_source_event_id",
        ),
        CheckConstraint(
            "label_type IN ('confirmed_correct', 'corrected', 'confirmed_incorrect')",
            name="ck_reviewed_examples_label_type",
        ),
        CheckConstraint(
            "(label_type = 'corrected' AND source_event_id IS NOT NULL) OR "
            "(label_type IN ('confirmed_correct', 'confirmed_incorrect') "
            "AND source_event_id IS NULL)",
            name="ck_reviewed_examples_review_source",
        ),
        CheckConstraint(
            "label_type NOT IN ('corrected', 'confirmed_incorrect') "
            "OR correction_reason IS NOT NULL",
            name="ck_reviewed_examples_review_reason",
        ),
        CheckConstraint(
            "label_type != 'confirmed_incorrect' OR reviewed_value_json IS NULL",
            name="ck_reviewed_examples_negative_value",
        ),
        CheckConstraint("is_reviewed", name="ck_reviewed_examples_is_reviewed"),
        Index(
            "ix_reviewed_examples_scope_active",
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
            "catalog_version",
            "is_reviewed",
            "is_valid",
        ),
        Index(
            "ix_reviewed_examples_tenant_last_seen",
            "tenant_id",
            "last_seen_at",
        ),
    )


class MemoryAdmissionDecisionRow(Base):
    __tablename__ = "memory_admission_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    assessment_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    conflict_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "example_id",
            "revision",
            name="uq_memory_admission_decisions_revision",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_memory_admission_decisions_idempotency",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'quarantined', 'rejected', "
            "'suspended', 'invalidated')",
            name="ck_memory_admission_decisions_status",
        ),
        CheckConstraint(
            "previous_status IS NULL OR previous_status IN ('pending', 'approved', "
            "'quarantined', 'rejected', 'suspended', 'invalidated')",
            name="ck_memory_admission_decisions_previous_status",
        ),
        CheckConstraint(
            "authority IN ('deterministic_policy', 'human_governor')",
            name="ck_memory_admission_decisions_authority",
        ),
        CheckConstraint("revision > 0", name="ck_memory_admission_decisions_revision"),
        Index(
            "ix_memory_admission_decisions_example_revision",
            "tenant_id",
            "example_id",
            "revision",
        ),
    )


class MemoryAdmissionRecordRow(Base):
    __tablename__ = "memory_admission_records"

    example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_decision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("memory_admission_decisions.decision_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'quarantined', 'rejected', "
            "'suspended', 'invalidated')",
            name="ck_memory_admission_records_status",
        ),
        CheckConstraint("revision > 0", name="ck_memory_admission_records_revision"),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_memory_admission_records_attempt_count",
        ),
        CheckConstraint(
            "(worker_id IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_memory_admission_records_lease",
        ),
        CheckConstraint(
            "(last_error_code IS NULL AND last_error_at IS NULL) "
            "OR (last_error_code IS NOT NULL AND last_error_at IS NOT NULL)",
            name="ck_memory_admission_records_last_error",
        ),
        Index(
            "ix_memory_admission_records_rebuild",
            "tenant_id",
            "schema_version",
            "status",
            "example_id",
        ),
        Index(
            "ix_memory_admission_records_worker_queue",
            "next_attempt_at",
            "lease_expires_at",
            "example_id",
        ),
    )


class MemoryQualityAssessmentRow(Base):
    __tablename__ = "memory_quality_assessments"

    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    recommendation: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    model_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    prompt_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("prompt_versions.prompt_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    advisory_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "example_id",
            "source",
            "policy_version",
            "input_fingerprint",
            name="uq_memory_quality_assessments_replay",
        ),
        CheckConstraint(
            "source IN ('deterministic', 'model_advisory')",
            name="ck_memory_quality_assessments_source",
        ),
        CheckConstraint(
            "recommendation IN ('recommend_approval', 'recommend_quarantine', "
            "'recommend_rejection')",
            name="ck_memory_quality_assessments_recommendation",
        ),
        CheckConstraint(
            "quality_score >= 0 AND quality_score <= 1",
            name="ck_memory_quality_assessments_score",
        ),
        CheckConstraint("advisory_only", name="ck_memory_quality_assessments_advisory"),
        CheckConstraint(
            "(source = 'model_advisory' AND model_version_id IS NOT NULL AND "
            "prompt_version_id IS NOT NULL) OR "
            "(source = 'deterministic' AND model_version_id IS NULL AND "
            "prompt_version_id IS NULL)",
            name="ck_memory_quality_assessments_versions",
        ),
        Index(
            "ix_memory_quality_assessments_example_time",
            "tenant_id",
            "example_id",
            "assessed_at",
        ),
    )


class MemoryQualitySignalRow(Base):
    __tablename__ = "memory_quality_signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assessment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("memory_quality_assessments.assessment_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(String(128), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    field_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_references_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "assessment_id",
            "ordinal",
            name="uq_memory_quality_signals_ordinal",
        ),
        CheckConstraint("ordinal >= 0", name="ck_memory_quality_signals_ordinal"),
        CheckConstraint(
            "source IN ('deterministic', 'model_advisory')",
            name="ck_memory_quality_signals_source",
        ),
        CheckConstraint(
            "verdict IN ('passed', 'warning', 'failed')",
            name="ck_memory_quality_signals_verdict",
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_memory_quality_signals_score",
        ),
    )


class MemoryConflictRow(Base):
    __tablename__ = "memory_conflicts"

    conflict_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    conflict_type: Mapped[str] = mapped_column(String(128), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    evidence_references_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    candidate_field_paths_json: Mapped[list[str]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_decision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_memory_conflicts_tenant_fingerprint",
        ),
        CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name="ck_memory_conflicts_status",
        ),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL AND resolution_decision_id IS NULL) "
            "OR (status IN ('resolved', 'dismissed') AND resolved_at IS NOT NULL AND "
            "resolution_decision_id IS NOT NULL)",
            name="ck_memory_conflicts_resolution",
        ),
        Index(
            "ix_memory_conflicts_scope_status",
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
            "status",
        ),
    )


class MemoryConflictResolutionDecisionRow(Base):
    __tablename__ = "memory_conflict_resolution_decisions"

    resolution_decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    conflict_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("memory_conflicts.conflict_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_status: Mapped[str] = mapped_column(String(32), nullable=False)
    selected_canonical_field_path: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_memory_conflict_resolution_decisions_idempotency",
        ),
        CheckConstraint(
            "previous_status = 'open'",
            name="ck_memory_conflict_resolution_decisions_previous_status",
        ),
        CheckConstraint(
            "target_status IN ('resolved', 'dismissed')",
            name="ck_memory_conflict_resolution_decisions_target_status",
        ),
        Index(
            "ix_memory_conflict_resolution_decisions_conflict",
            "tenant_id",
            "conflict_id",
            "decided_at",
        ),
    )


class MemoryConflictReevaluationRequestRow(Base):
    __tablename__ = "memory_conflict_reevaluation_requests"

    reevaluation_request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resolution_decision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "memory_conflict_resolution_decisions.resolution_decision_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "resolution_decision_id",
            "target_type",
            "target_id",
            name="uq_memory_conflict_reevaluation_requests_target",
        ),
        CheckConstraint(
            "target_type IN ('memory_admission', 'field_alias')",
            name="ck_memory_conflict_reevaluation_requests_target_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'requires_review', 'invalid_target')",
            name="ck_memory_conflict_reevaluation_requests_status",
        ),
        Index(
            "ix_memory_conflict_reevaluation_requests_queue",
            "tenant_id",
            "status",
            "target_type",
            "target_id",
        ),
    )


class MemoryConflictExampleRow(Base):
    __tablename__ = "memory_conflict_examples"

    conflict_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("memory_conflicts.conflict_id", ondelete="CASCADE"),
        primary_key=True,
    )
    example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "conflict_id",
            "ordinal",
            name="uq_memory_conflict_examples_ordinal",
        ),
        CheckConstraint("ordinal >= 0", name="ck_memory_conflict_examples_ordinal"),
        Index(
            "ix_memory_conflict_examples_lookup",
            "tenant_id",
            "example_id",
            "conflict_id",
        ),
    )


class ReviewerReliabilitySnapshotRow(Base):
    __tablename__ = "reviewer_reliability_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    profile_version: Mapped[str] = mapped_column(String(128), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    reviewed_fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    approved_fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    quarantined_fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_fact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reliability_score: Mapped[float] = mapped_column(Float, nullable=False)
    minimum_sample_met: Mapped[bool] = mapped_column(Boolean, nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "reviewer_id",
            "profile_version",
            name="uq_reviewer_reliability_snapshots_version",
        ),
        CheckConstraint(
            "reviewed_fact_count >= 0 AND approved_fact_count >= 0 AND "
            "quarantined_fact_count >= 0 AND rejected_fact_count >= 0 AND "
            "conflict_count >= 0",
            name="ck_reviewer_reliability_snapshots_counts",
        ),
        CheckConstraint(
            "approved_fact_count + quarantined_fact_count + rejected_fact_count "
            "<= reviewed_fact_count",
            name="ck_reviewer_reliability_snapshots_outcomes",
        ),
        CheckConstraint(
            "reliability_score >= 0 AND reliability_score <= 1",
            name="ck_reviewer_reliability_snapshots_score",
        ),
        Index(
            "ix_reviewer_reliability_snapshots_lookup",
            "tenant_id",
            "reviewer_id",
            "calculated_at",
        ),
    )


class FieldSemanticCatalogVersionRow(Base):
    __tablename__ = "field_semantic_catalog_versions"

    catalog_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    activation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    activation_idempotency_key_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "schema_version",
            "catalog_version",
            name="uq_field_semantic_catalog_versions_scope",
        ),
        UniqueConstraint(
            "tenant_id",
            "activation_idempotency_key_hash",
            name="uq_field_semantic_catalog_versions_activation_idempotency",
        ),
        CheckConstraint(
            "(is_active AND is_valid AND activated_by IS NOT NULL "
            "AND activation_reason IS NOT NULL "
            "AND activation_idempotency_key_hash IS NOT NULL "
            "AND activated_at IS NOT NULL AND retired_at IS NULL "
            "AND invalidated_at IS NULL) OR NOT is_active",
            name="ck_field_semantic_catalog_versions_active_metadata",
        ),
        CheckConstraint(
            "(is_valid AND invalidated_at IS NULL AND invalidated_reason IS NULL) OR "
            "(NOT is_valid AND NOT is_active AND invalidated_at IS NOT NULL "
            "AND invalidated_reason IS NOT NULL)",
            name="ck_field_semantic_catalog_versions_validity",
        ),
        Index(
            "uq_field_semantic_catalog_versions_active",
            "tenant_id",
            "schema_version",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
    )


class FieldAliasCandidateRow(Base):
    __tablename__ = "field_alias_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_field_path: Mapped[str] = mapped_column(Text, nullable=False)
    alias_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    context_anchors_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_collision: Mapped[bool] = mapped_column(Boolean, nullable=False)
    support_window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    promoted_catalog_version: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "(scope = 'tenant' AND tenant_id IS NOT NULL) OR "
            "(scope = 'global' AND tenant_id IS NULL)",
            name="ck_field_alias_candidates_scope",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_alias_candidates_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(status != 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND review_reason IS NOT NULL)",
            name="ck_field_alias_candidates_review_metadata",
        ),
        CheckConstraint(
            "(scope = 'global' AND promoted_catalog_version IS NULL) OR "
            "(scope = 'tenant' AND status IN ('approved', 'suspended') "
            "AND promoted_catalog_version IS NOT NULL) OR "
            "(scope = 'tenant' AND status IN ('pending', 'rejected') "
            "AND promoted_catalog_version IS NULL) OR "
            "(scope = 'tenant' AND status = 'invalidated')",
            name="ck_field_alias_candidates_promotion",
        ),
        CheckConstraint(
            "scope != 'global' OR promoted_catalog_version IS NULL",
            name="ck_field_alias_candidates_global_no_catalog",
        ),
        CheckConstraint(
            "support_window_days > 0",
            name="ck_field_alias_candidates_support_window",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_field_alias_candidates_revision",
        ),
        CheckConstraint(
            "updated_at >= submitted_at AND (reviewed_at IS NULL OR reviewed_at >= submitted_at)",
            name="ck_field_alias_candidates_timestamps",
        ),
        Index(
            "ix_field_alias_candidates_competing",
            "tenant_id",
            "schema_version",
            "document_type",
            "normalized_alias",
            "status",
        ),
    )


class FieldAliasCandidateSourceRow(Base):
    __tablename__ = "field_alias_candidate_sources"

    support_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_alias_candidates.candidate_id",
            name="fk_field_alias_sources_candidate",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_decision_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    template_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "evidence_id",
            "binding_decision_id",
            name="uq_field_alias_candidate_sources_review",
        ),
        Index(
            "ix_field_alias_candidate_sources_window",
            "tenant_id",
            "candidate_id",
            "occurred_at",
        ),
    )


class FieldAliasCandidateDecisionRow(Base):
    __tablename__ = "field_alias_candidate_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_alias_candidates.candidate_id",
            name="fk_field_alias_decisions_candidate",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    authority: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    promoted_catalog_version: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "candidate_id",
            "idempotency_key_hash",
            name="uq_field_alias_candidate_decisions_idempotency",
        ),
        CheckConstraint(
            "scope IN ('tenant', 'global')",
            name="ck_field_alias_candidate_decisions_scope",
        ),
        CheckConstraint(
            "previous_status IN ('pending', 'approved', 'rejected', 'suspended')",
            name="ck_field_alias_candidate_decisions_previous_status",
        ),
        CheckConstraint(
            "status IN ('approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_alias_candidate_decisions_status",
        ),
        CheckConstraint(
            "previous_status != status",
            name="ck_field_alias_candidate_decisions_transition",
        ),
        CheckConstraint(
            "previous_revision > 0 AND revision >= previous_revision",
            name="ck_field_alias_candidate_decisions_revisions",
        ),
        CheckConstraint(
            "(scope = 'tenant' AND authority = 'tenant_governor') OR "
            "(scope = 'global' AND authority = 'global_governor')",
            name="ck_field_alias_candidate_decisions_authority",
        ),
        CheckConstraint(
            "(scope = 'tenant' AND status = 'approved' "
            "AND promoted_catalog_version IS NOT NULL) OR "
            "(scope = 'global' AND status = 'approved' "
            "AND promoted_catalog_version IS NULL) OR "
            "(status != 'approved' AND promoted_catalog_version IS NULL)",
            name="ck_field_alias_candidate_decisions_promotion",
        ),
    )


class FieldAliasGlobalSupportSnapshotRow(Base):
    __tablename__ = "field_alias_global_support_snapshots"

    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_alias_candidates.candidate_id",
            name="fk_field_alias_global_support_candidate",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    salt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_fingerprints_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_documents: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_templates: Mapped[int] = mapped_column(Integer, nullable=False)
    distinct_reviewers: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "source_count >= 0 AND distinct_documents >= 0 "
            "AND distinct_templates >= 0 AND distinct_reviewers >= 0 "
            "AND distinct_documents <= source_count "
            "AND distinct_templates <= source_count "
            "AND distinct_reviewers <= source_count",
            name="ck_field_alias_global_support_counts",
        ),
    )


class MemoryConflictFieldAliasCandidateRow(Base):
    __tablename__ = "memory_conflict_field_alias_candidates"

    conflict_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "memory_conflicts.conflict_id",
            name="fk_memory_conflict_alias_candidates_conflict",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_alias_candidates.candidate_id",
            name="fk_memory_conflict_alias_candidates_candidate",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "conflict_id",
            "ordinal",
            name="uq_memory_conflict_field_alias_candidates_ordinal",
        ),
        CheckConstraint(
            "ordinal >= 0",
            name="ck_memory_conflict_field_alias_candidates_ordinal",
        ),
        Index(
            "ix_memory_conflict_field_alias_candidates_lookup",
            "tenant_id",
            "candidate_id",
            "conflict_id",
        ),
    )


class FieldSemanticAliasRow(Base):
    __tablename__ = "field_semantic_aliases"

    alias_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    catalog_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_semantic_catalog_versions.catalog_version_id",
            name="fk_field_aliases_catalog_version",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_field_path: Mapped[str] = mapped_column(Text, nullable=False)
    alias_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False)
    is_negative: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    submitted_by: Mapped[str] = mapped_column(String(128), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_evidence_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_binding_decision_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    submission_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "catalog_version_id",
            "document_type",
            "canonical_field_path",
            "normalized_alias",
            "is_negative",
            name="uq_field_semantic_aliases_semantics",
        ),
        UniqueConstraint(
            "tenant_id",
            "source_run_id",
            "source_evidence_id",
            "source_binding_decision_id",
            name="uq_field_semantic_aliases_review_source",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_semantic_aliases_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL "
            "AND review_reason IS NULL) OR "
            "(status != 'pending' AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
            "AND review_reason IS NOT NULL)",
            name="ck_field_semantic_aliases_review_metadata",
        ),
        CheckConstraint(
            "(status = 'invalidated' AND NOT is_valid) OR (status != 'invalidated' AND is_valid)",
            name="ck_field_semantic_aliases_validity",
        ),
        CheckConstraint(
            "(source_run_id IS NULL AND source_document_id IS NULL "
            "AND source_evidence_id IS NULL AND source_binding_decision_id IS NULL "
            "AND submission_reason IS NULL) OR "
            "(source_run_id IS NOT NULL AND source_document_id IS NOT NULL "
            "AND source_evidence_id IS NOT NULL "
            "AND source_binding_decision_id IS NOT NULL "
            "AND submission_reason IS NOT NULL)",
            name="ck_field_semantic_aliases_review_source",
        ),
        Index(
            "ix_field_semantic_aliases_binding",
            "tenant_id",
            "schema_version",
            "catalog_version_id",
            "document_type",
            "canonical_field_path",
            "status",
            "is_valid",
        ),
        Index(
            "ix_field_semantic_aliases_review_source",
            "tenant_id",
            "source_run_id",
            "source_document_id",
            "source_evidence_id",
        ),
    )


class FieldSemanticAliasDecisionRow(Base):
    __tablename__ = "field_semantic_alias_decisions"

    decision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    alias_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_semantic_aliases.alias_id",
            name="fk_field_alias_decisions_alias",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    previous_status: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "alias_id",
            "revision",
            name="uq_field_semantic_alias_decisions_revision",
        ),
        UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_field_semantic_alias_decisions_idempotency",
        ),
        CheckConstraint(
            "previous_status IN ('pending', 'approved', 'rejected', 'suspended')",
            name="ck_field_semantic_alias_decisions_previous_status",
        ),
        CheckConstraint(
            "status IN ('approved', 'rejected', 'suspended', 'invalidated')",
            name="ck_field_semantic_alias_decisions_status",
        ),
        CheckConstraint(
            "previous_status != status",
            name="ck_field_semantic_alias_decisions_transition",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_field_semantic_alias_decisions_revision",
        ),
        Index(
            "ix_field_semantic_alias_decisions_alias_revision",
            "tenant_id",
            "alias_id",
            "revision",
        ),
    )


class FieldSemanticContextAnchorRow(Base):
    __tablename__ = "field_semantic_context_anchors"

    anchor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    alias_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "field_semantic_aliases.alias_id",
            name="fk_field_context_anchors_alias",
            ondelete="CASCADE",
        ),
        primary_key=True,
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(String(32), nullable=False)
    max_distance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_negative: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "alias_id",
            "ordinal",
            name="uq_field_semantic_context_anchors_ordinal",
        ),
        CheckConstraint(
            "relation IN ('same_line', 'preceding', 'following', 'same_block', 'document_section')",
            name="ck_field_semantic_context_anchors_relation",
        ),
        CheckConstraint(
            "ordinal >= 0",
            name="ck_field_semantic_context_anchors_ordinal",
        ),
        CheckConstraint(
            "max_distance IS NULL OR max_distance > 0",
            name="ck_field_semantic_context_anchors_distance",
        ),
        Index(
            "ix_field_semantic_context_anchors_alias",
            "tenant_id",
            "alias_id",
            "ordinal",
        ),
    )


class FieldSemanticIndexVersionRow(Base):
    __tablename__ = "field_semantic_index_versions"

    index_version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    dense_model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sparse_model_version_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "version",
            name="uq_field_semantic_index_versions_tenant_version",
        ),
        CheckConstraint(
            "(is_valid AND invalidated_at IS NULL AND invalidated_reason IS NULL) OR "
            "(NOT is_valid AND NOT is_active AND invalidated_at IS NOT NULL "
            "AND invalidated_reason IS NOT NULL)",
            name="ck_field_semantic_index_versions_validity",
        ),
        Index(
            "uq_field_semantic_index_versions_active",
            "tenant_id",
            "schema_version",
            unique=True,
            postgresql_where=text("is_active"),
            sqlite_where=text("is_active = 1"),
        ),
        Index(
            "ix_field_semantic_index_versions_catalog",
            "tenant_id",
            "schema_version",
            "catalog_version",
            "is_valid",
        ),
    )


class FieldSemanticIndexProjectionRow(Base):
    __tablename__ = "field_semantic_index_projections"

    projection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    semantic_id: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("field_semantic_index_versions.index_version_id", ondelete="CASCADE"),
        nullable=False,
    )
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    canonical_field_path: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    catalog_version: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    approved_aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    negative_aliases_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    context_anchors_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    projection_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "semantic_id",
            "index_version_id",
            name="uq_field_semantic_index_projections_scope",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'indexed', 'failed', 'invalidated')",
            name="ck_field_semantic_index_projections_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_field_semantic_index_projections_attempts",
        ),
        CheckConstraint(
            "(status = 'processing' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND worker_id IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_field_semantic_index_projections_lease",
        ),
        Index(
            "ix_field_semantic_index_projections_lease",
            "tenant_id", "index_version_id", "status", "lease_expires_at",
        ),
        Index(
            "ix_field_semantic_index_projections_queue",
            "tenant_id",
            "index_version_id",
            "status",
            "updated_at",
        ),
        Index(
            "ix_field_semantic_index_projections_scope",
            "tenant_id",
            "document_type",
            "schema_version",
            "catalog_version",
            "status",
        ),
    )


class ExampleIndexProjectionRow(Base):
    __tablename__ = "example_index_projections"

    projection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    index_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("index_versions.index_version_id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    projection_checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "example_id",
            "index_version_id",
            name="uq_example_index_projections_scope",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'indexed', 'failed', 'invalidated')",
            name="ck_example_index_projections_status",
        ),
        CheckConstraint(
            "(status = 'processing' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'processing' AND worker_id IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)",
            name="ck_example_index_projections_lease",
        ),
        Index(
            "ix_example_index_projections_lease",
            "tenant_id", "index_version_id", "status", "lease_expires_at",
        ),
        Index(
            "ix_example_index_projections_queue",
            "tenant_id",
            "index_version_id",
            "status",
            "updated_at",
        ),
    )


class OCRProviderMetricEventRow(Base):
    __tablename__ = "ocr_provider_metric_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False)
    timeout_count: Mapped[int] = mapped_column(Integer, nullable=False)
    circuit_open_count: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    other_error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    text_box_count: Mapped[int] = mapped_column(Integer, nullable=False)
    empty_rate_numerator: Mapped[int] = mapped_column(Integer, nullable=False)
    empty_rate_denominator: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "page_count >= 0 AND latency_ms >= 0 AND success_count >= 0 "
            "AND timeout_count >= 0 AND circuit_open_count >= 0 "
            "AND schema_error_count >= 0 AND other_error_count >= 0 "
            "AND text_box_count >= 0 AND empty_rate_numerator >= 0 "
            "AND empty_rate_denominator >= 0",
            name="ck_ocr_provider_metric_events_counts",
        ),
        Index(
            "ix_ocr_provider_metric_events_provider_created",
            "provider_name",
            "created_at",
        ),
    )


class OCRPageMetricEventRow(Base):
    __tablename__ = "ocr_page_metric_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_event_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("ocr_provider_metric_events.event_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    text_box_count: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "page_number > 0 AND latency_ms >= 0 AND text_box_count >= 0",
            name="ck_ocr_page_metric_events_values",
        ),
    )


class OCRComparisonMetricEventRow(Base):
    __tablename__ = "ocr_comparison_metric_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    raw_observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    bound_field_count: Mapped[int] = mapped_column(Integer, nullable=False)
    corroborated_count: Mapped[int] = mapped_column(Integer, nullable=False)
    conflicting_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ocr_only_count: Mapped[int] = mapped_column(Integer, nullable=False)
    vision_only_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unresolved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unavailable_count: Mapped[int] = mapped_column(Integer, nullable=False)
    conflict_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "raw_observation_count >= 0 AND bound_field_count >= 0 "
            "AND corroborated_count >= 0 AND conflicting_count >= 0 "
            "AND ocr_only_count >= 0 AND vision_only_count >= 0 "
            "AND unresolved_count >= 0 AND unavailable_count >= 0",
            name="ck_ocr_comparison_metric_events_counts",
        ),
        Index("ix_ocr_comparison_metric_events_created", "created_at"),
    )


class HardNegativeRetrievalJudgmentRow(Base):
    __tablename__ = "hard_negative_retrieval_judgments"

    judgment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    query_example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    retrieved_example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    scores_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    rejection_reason: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    is_relevant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    judgment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_hard_negative_judgments_tenant_fingerprint",
        ),
        CheckConstraint("rank > 0", name="ck_hard_negative_judgments_rank"),
        CheckConstraint("NOT is_relevant", name="ck_hard_negative_judgments_rejected"),
        CheckConstraint(
            "query_example_id != retrieved_example_id",
            name="ck_hard_negative_judgments_distinct_examples",
        ),
        Index(
            "ix_hard_negative_judgments_scope",
            "tenant_id",
            "schema_version",
            "is_valid",
            "judgment_id",
        ),
    )


class HardNegativeCandidateRow(Base):
    __tablename__ = "hard_negative_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    positive_example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    negative_example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="CASCADE"),
        nullable=False,
    )
    signal_types_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    proposal_source: Mapped[str] = mapped_column(String(48), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    source_judgment_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("hard_negative_retrieval_judgments.judgment_id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    candidate_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_hard_negative_candidates_tenant_fingerprint",
        ),
        UniqueConstraint(
            "tenant_id",
            "review_id",
            name="uq_hard_negative_candidates_tenant_review",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_hard_negative_candidates_status",
        ),
        CheckConstraint(
            "positive_example_id != negative_example_id",
            name="ck_hard_negative_candidates_distinct_examples",
        ),
        CheckConstraint(
            "(status = 'pending' AND review_id IS NULL AND reviewer_id IS NULL "
            "AND review_reason IS NULL AND reviewed_at IS NULL) OR "
            "(status IN ('approved', 'rejected') AND review_id IS NOT NULL "
            "AND reviewer_id IS NOT NULL AND review_reason IS NOT NULL "
            "AND reviewed_at IS NOT NULL)",
            name="ck_hard_negative_candidates_human_gate",
        ),
        Index(
            "ix_hard_negative_candidates_export",
            "tenant_id",
            "schema_version",
            "status",
            "is_valid",
            "candidate_id",
        ),
    )


class TrainingDatasetVersionRow(Base):
    __tablename__ = "training_dataset_versions"

    dataset_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_tenant_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    redaction_policy_version: Mapped[str] = mapped_column(String(128), nullable=False)
    split_rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    split_salt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    cross_tenant: Mapped[bool] = mapped_column(Boolean, nullable=False)
    authorization_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_scope",
            "dataset_id",
            "dataset_version",
            name="uq_training_dataset_versions_scope_identity",
        ),
        UniqueConstraint(
            "tenant_scope",
            "fingerprint",
            name="uq_training_dataset_versions_scope_fingerprint",
        ),
        CheckConstraint(
            "status IN ('building', 'exported', 'failed')",
            name="ck_training_dataset_versions_status",
        ),
        CheckConstraint("record_count > 0", name="ck_training_dataset_versions_records"),
        CheckConstraint(
            "(cross_tenant AND authorization_id IS NOT NULL) OR "
            "(NOT cross_tenant AND authorization_id IS NULL)",
            name="ck_training_dataset_versions_authorization",
        ),
        Index(
            "ix_training_dataset_versions_scope_schema",
            "tenant_scope",
            "schema_version",
            "status",
        ),
    )


class TrainingDatasetRecordRow(Base):
    __tablename__ = "training_dataset_records"

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_key: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("training_dataset_versions.dataset_key", ondelete="CASCADE"),
        nullable=False,
    )
    source_tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    split: Mapped[str] = mapped_column(String(32), nullable=False)
    source_document_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    candidate_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    group_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    record_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "dataset_key",
            "fingerprint",
            name="uq_training_dataset_records_dataset_fingerprint",
        ),
        CheckConstraint(
            "split IN ('train', 'validation', 'evaluation')",
            name="ck_training_dataset_records_split",
        ),
        Index(
            "ix_training_dataset_records_dataset_split",
            "dataset_key",
            "split",
            "record_id",
        ),
    )


class TrainingRunRow(Base):
    __tablename__ = "training_runs"

    training_run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    training_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    training_dataset_key: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("training_dataset_versions.dataset_key", ondelete="RESTRICT"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    candidate_model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_job_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    run_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "training_run_id",
            name="uq_training_runs_tenant_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "candidate_model_version_id",
            name="uq_training_runs_tenant_candidate_model_version",
        ),
        CheckConstraint(
            "target_type IN ('reranker', 'embedding', 'vision_generation')",
            name="ck_training_runs_target_type",
        ),
        CheckConstraint(
            "status IN ('created', 'export_ready', 'submitted', 'queued', 'running', "
            "'canceling', 'succeeded', 'failed', 'canceled', 'unsupported')",
            name="ck_training_runs_status",
        ),
        Index(
            "ix_training_runs_worker_queue",
            "status",
            "provider",
            "created_at",
        ),
    )


class ModelArtifactRow(Base):
    __tablename__ = "model_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    training_run_key: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("training_runs.training_run_key", ondelete="RESTRICT"),
        nullable=False,
    )

    model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "training_run_key",
            name="uq_model_artifacts_tenant_training_run",
        ),
        UniqueConstraint(
            "tenant_id",
            "model_version_id",
            name="uq_model_artifacts_tenant_model_version",
        ),
        CheckConstraint(
            "stage IN ('registered', 'offline_evaluation', 'shadow', 'canary', "
            "'production', 'rolled_back', 'rejected')",
            name="ck_model_artifacts_stage",
        ),
        Index("ix_model_artifacts_tenant_stage", "tenant_id", "stage", "is_valid"),
    )


class PromotionCandidateRow(Base):
    __tablename__ = "promotion_candidates"

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    artifact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_evaluation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    evaluation_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(256), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metric_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    hard_failure_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    compatibility_errors_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "candidate_id", name="uq_promotion_candidates_tenant_id"),
        CheckConstraint(
            "(artifact_id IS NULL AND model_evaluation_id IS NULL) OR "
            "(artifact_id IS NOT NULL AND model_evaluation_id IS NOT NULL)",
            name="ck_promotion_candidates_evidence_pair",
        ),
        CheckConstraint(
            "status IN ('shadow', 'canary', 'active', 'rollback', 'rejected')",
            name="ck_promotion_candidates_status",
        ),
        CheckConstraint("revision > 0", name="ck_promotion_candidates_revision"),
        Index("ix_promotion_candidates_scope_status", "tenant_id", "status", "updated_at"),
        Index(
            "uq_promotion_candidates_tenant_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class PromotionCandidateAuditRow(Base):
    __tablename__ = "promotion_candidate_audits"

    audit_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("promotion_candidates.candidate_id", ondelete="RESTRICT"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ModelEvaluationRow(Base):
    __tablename__ = "model_evaluations"

    model_evaluation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_evaluation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_artifacts.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_model_version_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    prompt_version: Mapped[str] = mapped_column(String(256), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "model_evaluation_id",
            name="uq_model_evaluations_tenant_identity",
        ),
        CheckConstraint(
            "stage IN ('offline_evaluation', 'shadow', 'canary', 'production')",
            name="ck_model_evaluations_stage",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'failed')",
            name="ck_model_evaluations_status",
        ),
        Index(
            "ix_model_evaluations_artifact_stage",
            "tenant_id",
            "artifact_id",
            "stage",
            "created_at",
        ),
    )


class ModelDeploymentRow(Base):
    __tablename__ = "model_deployments"

    deployment_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_version_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_versions.model_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    traffic_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    previous_deployment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deployment_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "deployment_id",
            name="uq_model_deployments_tenant_identity",
        ),
        CheckConstraint(
            "target_type IN ('reranker', 'embedding', 'vision_generation')",
            name="ck_model_deployments_target_type",
        ),
        CheckConstraint(
            "stage IN ('shadow', 'canary', 'production')",
            name="ck_model_deployments_stage",
        ),
        CheckConstraint(
            "status IN ('requested', 'active', 'failed', 'superseded', 'rolled_back')",
            name="ck_model_deployments_status",
        ),
        CheckConstraint(
            "traffic_percentage >= 0 AND traffic_percentage <= 100",
            name="ck_model_deployments_traffic",
        ),
        Index(
            "uq_model_deployments_tenant_active_production",
            "tenant_id",
            "target_type",
            unique=True,
            postgresql_where=text("stage = 'production' AND status = 'active'"),
            sqlite_where=text("stage = 'production' AND status = 'active'"),
        ),
    )


class PromotionDecisionRow(Base):
    __tablename__ = "promotion_decisions"

    decision_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    artifact_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("model_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "decision_id",
            name="uq_promotion_decisions_tenant_identity",
        ),
        CheckConstraint(
            "decision IN ('approved', 'rejected', 'rollback_required')",
            name="ck_promotion_decisions_decision",
        ),
        Index(
            "ix_promotion_decisions_artifact",
            "tenant_id",
            "artifact_id",
            "decided_at",
        ),
    )


class EvaluationDatasetRow(Base):
    __tablename__ = "evaluation_datasets"

    dataset_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "dataset_id",
            "dataset_version",
            name="uq_evaluation_datasets_tenant_identity",
        ),
        Index(
            "ix_evaluation_datasets_tenant_schema",
            "tenant_id",
            "schema_version",
        ),
    )


class EvaluationRunRow(Base):
    __tablename__ = "evaluation_runs"

    evaluation_run_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_key: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("evaluation_datasets.dataset_key", ondelete="RESTRICT"),
        nullable=False,
    )
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(256), nullable=False)
    model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(256), nullable=False)
    retrieval_policy_version: Mapped[str] = mapped_column(String(256), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    run_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "evaluation_run_id",
            name="uq_evaluation_runs_tenant_identity",
        ),
        CheckConstraint(
            "status IN ('created', 'running', 'completed', 'failed')",
            name="ck_evaluation_runs_status",
        ),
        Index(
            "ix_evaluation_runs_tenant_dataset",
            "tenant_id",
            "dataset_id",
            "dataset_version",
        ),
    )


class EvaluationReportArtifactRow(Base):
    __tablename__ = "evaluation_report_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_run_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("evaluation_runs.evaluation_run_key", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    report_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    report_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_key", "report_kind",
            name="uq_evaluation_report_run_kind",
        ),
        CheckConstraint(
            "report_kind IN ('json', 'markdown')",
            name="ck_evaluation_report_kind",
        ),
        Index("ix_evaluation_report_tenant_run", "tenant_id", "evaluation_run_key"),
    )


class MemoryGovernanceAuditRow(Base):
    __tablename__ = "memory_governance_audits"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resource_version: Mapped[str | None] = mapped_column(String(256), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "action",
            "idempotency_key_hash",
            name="uq_memory_governance_audits_idempotency",
        ),
        CheckConstraint(
            "action IN ('approve_admission', 'reject_admission', "
            "'quarantine_admission', 'requeue_admission', 'approve_field_alias', "
            "'disable_field_alias', 'resolve_conflict', 'dismiss_conflict', "
            "'disable_example', 'invalidate_schema', 'rebuild_index', "
            "'submit_feedback')",
            name="ck_memory_governance_audits_action",
        ),
        Index(
            "ix_memory_governance_audits_resource",
            "tenant_id",
            "resource_type",
            "resource_id",
            "created_at",
        ),
        Index(
            "ix_memory_governance_audits_trace",
            "tenant_id",
            "trace_id",
            "created_at",
        ),
    )


class RetrievalTraceRow(Base):
    __tablename__ = "retrieval_traces"

    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_type: Mapped[str] = mapped_column(String(128), nullable=False)
    field_path: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str | None] = mapped_column(String(256), nullable=True)
    dense_model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    sparse_model_version: Mapped[str | None] = mapped_column(String(256), nullable=True)
    rerank_model_version: Mapped[str | None] = mapped_column(String(256), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(256), nullable=False)
    retrieval_policy_version: Mapped[str] = mapped_column(String(256), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(256), nullable=False)
    stage_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dense_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sparse_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    rerank_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    positive_result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    negative_result_count: Mapped[int] = mapped_column(Integer, nullable=False)
    positive_example_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    negative_example_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    empty_retrieval: Mapped[bool] = mapped_column(Boolean, nullable=False)
    positive_hit_rate: Mapped[float] = mapped_column(Float, nullable=False)
    negative_hit_rate: Mapped[float] = mapped_column(Float, nullable=False)
    review_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    remote_model_error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "dense_candidate_count >= 0 AND sparse_candidate_count >= 0 "
            "AND rerank_candidate_count >= 0 AND positive_result_count >= 0 "
            "AND negative_result_count >= 0 AND remote_model_error_count >= 0",
            name="ck_retrieval_traces_nonnegative_counts",
        ),
        CheckConstraint(
            "positive_hit_rate >= 0 AND positive_hit_rate <= 1 "
            "AND negative_hit_rate >= 0 AND negative_hit_rate <= 1",
            name="ck_retrieval_traces_hit_rates",
        ),
        CheckConstraint(
            "(succeeded AND error_code IS NULL) OR (NOT succeeded AND error_code IS NOT NULL)",
            name="ck_retrieval_traces_outcome",
        ),
        Index(
            "ix_retrieval_traces_tenant_index_created",
            "tenant_id",
            "index_version",
            "created_at",
        ),
        Index(
            "ix_retrieval_traces_tenant_scope",
            "tenant_id",
            "document_type",
            "field_path",
            "schema_version",
        ),
    )


class MemoryRetrievalFeedbackRow(Base):
    __tablename__ = "memory_retrieval_feedback"

    feedback_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("retrieval_traces.trace_id", ondelete="RESTRICT"),
        nullable=False,
    )
    example_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("reviewed_examples.example_id", ondelete="RESTRICT"),
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "fingerprint",
            name="uq_memory_retrieval_feedback_fingerprint",
        ),
        CheckConstraint(
            "label IN ('helpful', 'not_relevant', 'misleading')",
            name="ck_memory_retrieval_feedback_label",
        ),
        CheckConstraint(
            "label = 'helpful' OR reason IS NOT NULL",
            name="ck_memory_retrieval_feedback_reason",
        ),
        Index(
            "ix_memory_retrieval_feedback_trace",
            "tenant_id",
            "trace_id",
            "created_at",
        ),
    )


class AuthTenantRow(Base):
    __tablename__ = "auth_tenants"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthUserRow(Base):
    __tablename__ = "auth_users"

    subject: Mapped[str] = mapped_column(String(256), primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthRoleRow(Base):
    __tablename__ = "auth_roles"

    role_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(String(512), nullable=False)


class AuthPermissionRow(Base):
    __tablename__ = "auth_permissions"

    permission_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    description: Mapped[str] = mapped_column(String(512), nullable=False)


class AuthUserTenantRow(Base):
    __tablename__ = "auth_user_tenants"

    subject: Mapped[str] = mapped_column(
        String(256), ForeignKey("auth_users.subject", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("auth_tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )


class AuthRolePermissionRow(Base):
    __tablename__ = "auth_role_permissions"

    role_name: Mapped[str] = mapped_column(
        String(128), ForeignKey("auth_roles.role_name", ondelete="CASCADE"), primary_key=True
    )
    permission_name: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("auth_permissions.permission_name", ondelete="CASCADE"),
        primary_key=True,
    )


class AuthUserRoleRow(Base):
    __tablename__ = "auth_user_roles"

    subject: Mapped[str] = mapped_column(
        String(256), ForeignKey("auth_users.subject", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("auth_tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    role_name: Mapped[str] = mapped_column(
        String(128), ForeignKey("auth_roles.role_name", ondelete="CASCADE"), primary_key=True
    )


class SecurityAuditEventRow(Base):
    __tablename__ = "security_audit_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    required_permission: Mapped[str | None] = mapped_column(String(128), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_path: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('authentication_failed', 'authorization_denied', "
            "'identity_override_rejected', 'resource_not_found_or_cross_tenant')",
            name="ck_security_audit_events_type",
        ),
        Index(
            "ix_security_audit_events_tenant_time",
            "tenant_id",
            "occurred_at",
        ),
        Index(
            "ix_security_audit_events_trace",
            "trace_id",
            "occurred_at",
        ),
    )


class IdempotencyRequestRow(Base):
    __tablename__ = "idempotency_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "operation",
            "idempotency_key",
            name="uq_idempotency_requests_operation_key",
        ),
    )


class AccountingCandidateRow(Base):
    __tablename__ = "accounting_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("extraction_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tax_rule_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chart_of_accounts_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    posting_rule_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", name="uq_accounting_candidates_tenant_run"),
        CheckConstraint(
            "status IN ('pending_rule_review', 'approved', 'rejected', 'posted')",
            name="accounting_candidates_status",
        ),
        CheckConstraint("revision >= 1", name="accounting_candidates_revision"),
    )


class TaxAssessmentRow(Base):
    __tablename__ = "tax_assessments"

    assessment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("accounting_candidates.candidate_id", ondelete="RESTRICT"),
        unique=True,
    )
    tax_rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    tax_code: Mapped[str] = mapped_column(String(128), nullable=False)
    taxable_amount: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False)
    tax_amount: Mapped[Any] = mapped_column(Numeric(24, 8), nullable=False)
    advisory_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PostingProposalRow(Base):
    __tablename__ = "posting_proposals"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("accounting_candidates.candidate_id", ondelete="RESTRICT"),
        unique=True,
    )
    chart_of_accounts_version: Mapped[str] = mapped_column(String(128), nullable=False)
    posting_rule_version: Mapped[str] = mapped_column(String(128), nullable=False)
    lines_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    decided_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExchangeRateSnapshotRow(Base):
    __tablename__ = "exchange_rate_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    rate: Mapped[Any] = mapped_column(Numeric(30, 12), nullable=False)
    precision: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountingPostingAttemptRow(Base):
    __tablename__ = "accounting_posting_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounting_candidates.candidate_id", ondelete="RESTRICT")
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    external_reference: Mapped[str | None] = mapped_column(String(256), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountingAuditRow(Base):
    __tablename__ = "accounting_audits"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("accounting_candidates.candidate_id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("candidate_id", "revision", name="uq_accounting_audits_revision"),
    )


class TrainingDatasetExportRow(Base):
    __tablename__ = "training_dataset_exports"

    export_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_key: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("training_dataset_versions.dataset_key", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    tenant_scope: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    manifest_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    positive_count: Mapped[int] = mapped_column(Integer, nullable=False)
    hard_negative_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    export_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_scope",
            "dataset_id",
            "dataset_version",
            name="uq_training_dataset_exports_identity",
        ),
        CheckConstraint("record_count > 0", name="ck_training_dataset_exports_records"),
        CheckConstraint("positive_count > 0", name="ck_training_dataset_exports_positive"),
        CheckConstraint("hard_negative_count >= 0", name="ck_training_dataset_exports_negatives"),
    )


class TrainingJobRow(Base):
    __tablename__ = "training_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_export_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("training_dataset_exports.export_id", ondelete="RESTRICT")
    )
    training_run_key: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("training_runs.training_run_key", ondelete="RESTRICT"), nullable=True
    )
    training_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    remote_job_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    retry_of_job_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("training_jobs.job_id", ondelete="RESTRICT"), nullable=True
    )
    idempotency_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_submit_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    operation_cancel_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    operation_commit_id: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    job_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "training_run_id", name="uq_training_jobs_run"),
        UniqueConstraint("tenant_id", "idempotency_digest", name="uq_training_jobs_idempotency"),
        CheckConstraint(
            "status IN ('planned','submitted','running','succeeded','failed',"
            "'cancelled','quarantined')",
            name="ck_training_jobs_status",
        ),
        CheckConstraint("revision > 0", name="ck_training_jobs_revision"),
        CheckConstraint(
            "claim_count >= 0 AND failure_attempt_count >= 0",
            name="ck_training_jobs_attempts",
        ),
        Index(
            "ix_training_jobs_worker_queue",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "created_at",
        ),
    )


class TrainingArtifactRow(Base):
    __tablename__ = "training_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("training_jobs.job_id", ondelete="RESTRICT"), unique=True
    )
    training_run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    remote_job_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "checksum_sha256", name="uq_training_artifacts_checksum"),
        CheckConstraint("size_bytes > 0", name="ck_training_artifacts_size"),
    )


class TrainingAuditEventRow(Base):
    __tablename__ = "training_audit_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("training_jobs.job_id", ondelete="RESTRICT")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("job_id", "revision", name="uq_training_audit_job_revision"),
    )


class EvaluationSnapshotRow(Base):
    __tablename__ = "evaluation_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    cases_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "dataset_version", name="uq_evaluation_snapshot_version"),
    )


class EvaluationJobRow(Base):
    __tablename__ = "evaluation_jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("evaluation_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=True,
    )
    dataset_key: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("evaluation_datasets.dataset_key", ondelete="RESTRICT"),
    )
    dataset_id: Mapped[str | None] = mapped_column(String(128))
    evidence_class: Mapped[str] = mapped_column(
        String(32), nullable=False, default="diagnostic_only",
    )
    suite: Mapped[str | None] = mapped_column(String(64))
    retrieval_policy_version: Mapped[str | None] = mapped_column(String(128))
    catalog_version: Mapped[str | None] = mapped_column(String(128))
    admission_policy_version: Mapped[str | None] = mapped_column(String(128))
    field_binding_policy_version: Mapped[str | None] = mapped_column(String(128))
    evaluation_run_id: Mapped[str | None] = mapped_column(String(128))
    dataset_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(256), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key_hash", name="uq_evaluation_job_idempotency"),
        UniqueConstraint("evaluation_run_id", name="uq_evaluation_jobs_run_id"),
        CheckConstraint(
            "(evidence_class = 'diagnostic_only' AND snapshot_id IS NOT NULL "
            "AND dataset_key IS NULL AND dataset_id IS NULL AND suite IS NULL "
            "AND retrieval_policy_version IS NULL) OR "
            "(evidence_class = 'suite_run' AND snapshot_id IS NULL "
            "AND dataset_key IS NOT NULL AND dataset_id IS NOT NULL AND suite IN "
            "('case_rag', 'trusted_memory_field_binding') "
            "AND retrieval_policy_version IS NOT NULL)",
            name="ck_evaluation_job_evidence_binding",
        ),
        CheckConstraint(
            "(evidence_class = 'diagnostic_only' AND evaluation_run_id IS NULL) OR "
            "(evidence_class = 'suite_run' AND "
            "((status = 'completed' AND evaluation_run_id IS NOT NULL) OR "
            "(status <> 'completed' AND evaluation_run_id IS NULL)))",
            name="ck_evaluation_job_run_link",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'quarantined')",
            name="ck_evaluation_job_status",
        ),
        CheckConstraint(
            "(status = 'running' AND worker_id IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'running' AND worker_id IS NULL AND lease_token IS NULL "
            "AND lease_expires_at IS NULL)", name="ck_evaluation_job_lease",
        ),
        Index("ix_evaluation_jobs_queue", "status", "next_attempt_at", "lease_expires_at"),
    )


class EvaluationScheduleRow(Base):
    __tablename__ = "evaluation_schedules"

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("evaluation_snapshots.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
    )
    index_version: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(256), nullable=False)
    threshold_version: Mapped[str] = mapped_column(String(128), nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        CheckConstraint("interval_seconds >= 3600", name="ck_evaluation_schedule_interval"),
        Index("ix_evaluation_schedules_due", "enabled", "next_run_at"),
    )


class EvaluationJobReportRow(Base):
    __tablename__ = "evaluation_job_reports"

    report_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("evaluation_jobs.job_id", ondelete="RESTRICT"),
        nullable=False, unique=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    metrics_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationJobArtifactRow(Base):
    __tablename__ = "evaluation_job_artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("evaluation_jobs.job_id", ondelete="RESTRICT"),
        nullable=False, unique=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reference: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CodeHarnessTaskRow(Base):
    __tablename__ = "code_harness_tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    versions_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    budget_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key_hash", name="uq_code_harness_task_idempotency"),
        Index("ix_code_harness_tasks_scope", "tenant_id", "repository_id", "status"),
    )


class CodeHarnessSourceRow(Base):
    __tablename__ = "code_harness_sources"

    repository_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    repository_version: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_code_harness_sources_revision"),
        Index("ix_code_harness_sources_tenant_enabled", "tenant_id", "enabled"),
    )


class CodeHarnessAttemptRow(Base):
    __tablename__ = "code_harness_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_harness_tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_code_harness_attempt_number"),
    )


class CodeHarnessSnapshotRow(Base):
    __tablename__ = "code_harness_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(256), nullable=False)
    manifest_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    files_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    versions_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "repository_id", "revision", name="uq_code_harness_snapshot_revision"),
    )


class CodeHarnessPatchRow(Base):
    __tablename__ = "code_harness_patches"

    patch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    repository_id: Mapped[str] = mapped_column(String(128), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operations_json: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    patch_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CodeHarnessExecutionRow(Base):
    __tablename__ = "code_harness_executions"

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("code_harness_tasks.task_id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_summary: Mapped[str] = mapped_column(String(4096), nullable=False)
    error_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "attempt_id",
            name="uq_code_harness_execution_attempt",
        ),
    )


class CodeHarnessWatchdogRow(Base):
    __tablename__ = "code_harness_watchdogs"

    observation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    error_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    changed_ast_fingerprint: Mapped[bool] = mapped_column(Boolean, nullable=False)
    changed_symbols: Mapped[bool] = mapped_column(Boolean, nullable=False)
    changed_diagnostics: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CodeHarnessPostmortemRow(Base):
    __tablename__ = "code_harness_postmortems"

    postmortem_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    version_scope: Mapped[str] = mapped_column(String(256), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    admission_status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_signature: Mapped[str | None] = mapped_column(String(128), nullable=True)
    root_cause: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    solution_pattern: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    affected_language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    affected_symbol_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patch_shape: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_trace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_event_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "fingerprint", "version_scope",
            name="uq_code_harness_postmortem_scope",
        ),
    )


class CodeHarnessPostmortemSourceEventRow(Base):
    __tablename__ = "code_harness_postmortem_source_events"

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    postmortem_id: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    version_scope: Mapped[str] = mapped_column(String(256), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_summary: Mapped[str] = mapped_column(String(512), nullable=False)
    payload_checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_repository_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_snapshot_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_revision: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_patch_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CodeHarnessAuditRow(Base):
    __tablename__ = "code_harness_audits"

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CodeHarnessProjectionRow(Base):
    __tablename__ = "code_harness_projections"

    projection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    index_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "snapshot_id", "index_version", name="uq_code_harness_projection"),
    )
