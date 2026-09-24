"""Validated application settings."""

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Environment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


def _default_admission_quality_scores() -> dict[
    Literal["low", "standard", "high", "critical"], float
]:
    return {"low": 0.80, "standard": 0.85, "high": 0.95, "critical": 1.0}


class ValidationFieldOverrideSettings(BaseModel):
    """Sparse field-level validation threshold overrides parsed from JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accept_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    reject_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    min_clarity: float | None = Field(default=None, ge=0.0, le=1.0)
    min_width: int | None = Field(default=None, gt=0)
    min_height: int | None = Field(default=None, gt=0)
    ocr_match_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    amount_tolerance: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_local_route_thresholds(self) -> "ValidationFieldOverrideSettings":
        if (
            self.accept_threshold is not None
            and self.reject_threshold is not None
            and self.reject_threshold >= self.accept_threshold
        ):
            raise ValueError("reject_threshold must be lower than accept_threshold")
        return self


class TenantRedactionPolicySettings(BaseModel):
    """Tenant-specific policy for derived reviewed-example projections."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Literal["none", "mask", "hash", "drop"] = "mask"
    hash_salt: SecretStr | None = None

    @model_validator(mode="after")
    def validate_hash_salt(self) -> "TenantRedactionPolicySettings":
        if self.strategy == "hash" and self.hash_salt is None:
            raise ValueError("hash_salt is required for tenant hash redaction")
        return self


class ExampleRetrievalCategorySettings(BaseModel):
    """Default candidate, result, threshold, and fusion policy for one label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_k: int = Field(default=24, gt=0, le=500)
    top_k: int = Field(default=3, gt=0, le=20)
    min_relevance_score: float = Field(default=0.0, ge=0.0)
    dense_weight: float | None = Field(default=None, ge=0.0)
    sparse_weight: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_limits(self) -> "ExampleRetrievalCategorySettings":
        if self.top_k > self.candidate_k:
            raise ValueError("example retrieval top_k must not exceed candidate_k")
        return self


class ExampleRetrievalCategoryOverrideSettings(BaseModel):
    """Sparse exact-field override for one reviewed-example label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_k: int | None = Field(default=None, gt=0, le=500)
    top_k: int | None = Field(default=None, gt=0, le=20)
    min_relevance_score: float | None = Field(default=None, ge=0.0)
    dense_weight: float | None = Field(default=None, ge=0.0)
    sparse_weight: float | None = Field(default=None, ge=0.0)


class QwenModelCostSettings(BaseModel):
    """Operator-supplied cost rates; no provider pricing is guessed in code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_million_tokens: float = Field(ge=0.0)
    output_per_million_tokens: float = Field(ge=0.0)


class PaddleXCompatibleOCRProviderSettings(BaseModel):
    """One independently deployed PaddleX-compatible ``/ocr`` provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    provider_name: str = Field(min_length=1, max_length=128)
    base_url: str
    endpoint_path: str = "/ocr"
    provider_version: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=256)
    config_version: str = Field(min_length=1, max_length=128)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    read_timeout_seconds: float = Field(default=30.0, gt=0)
    write_timeout_seconds: float = Field(default=30.0, gt=0)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_concurrency: int = Field(default=4, gt=0, le=100)
    requests_per_minute: int = Field(default=60, gt=0)
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=8.0, gt=0)
    circuit_failure_threshold: int = Field(default=5, gt=0)
    circuit_recovery_seconds: float = Field(default=30.0, gt=0)
    max_input_bytes: int = Field(default=26_214_400, gt=0)

    @field_validator(
        "provider_name",
        "provider_version",
        "model_version",
        "config_version",
    )
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized != value:
            raise ValueError("OCR provider identifiers must be non-empty and normalized")
        return normalized

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("endpoint_path")
    @classmethod
    def normalize_endpoint_path(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_provider(self) -> "PaddleXCompatibleOCRProviderSettings":
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OCR provider base_url must be an absolute HTTP(S) URL")
        if not self.endpoint_path.startswith("/"):
            raise ValueError("OCR provider endpoint_path must start with '/'")
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("OCR provider backoff max must not be lower than base")
        return self


def _postgres_database_identity(dsn: str) -> tuple[str, int, str]:
    normalized = dsn.replace("postgresql+psycopg://", "postgresql://", 1)
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("PostgreSQL DSN must use postgres or postgresql scheme")
    if parsed.hostname is None:
        raise ValueError("PostgreSQL DSN must include a hostname")
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise ValueError("PostgreSQL DSN must include a database name")
    return (parsed.hostname.lower(), parsed.port or 5432, database)


def _resolve_business_database_url(
    raw_url: SecretStr,
    password_file: Path | None,
    environment: Environment,
) -> SecretStr:
    """Keep Docker secret contents out of process environment and diagnostics."""

    try:
        url = make_url(raw_url.get_secret_value())
    except (ArgumentError, ValueError) as exc:
        raise ValueError("business_database_url is invalid") from exc
    if password_file is None:
        if environment is Environment.PRODUCTION:
            raise ValueError("Production requires business_database_password_file")
        return raw_url
    if url.get_backend_name() != "postgresql":
        raise ValueError("business_database_password_file requires PostgreSQL")
    if url.password is not None:
        raise ValueError("Set a database URL password or password file, not both")
    if environment is Environment.PRODUCTION and not password_file.is_absolute():
        raise ValueError("Production business_database_password_file must be absolute")
    try:
        with password_file.open("r", encoding="utf-8") as source:
            content = source.read(4097)
    except (OSError, UnicodeError) as exc:
        raise ValueError("business_database_password_file is invalid or unreadable") from exc
    if len(content) > 4096:
        raise ValueError("business_database_password_file is invalid or unreadable")
    password = content.rstrip("\r\n")
    if not password or any(character in password for character in "\r\n\x00"):
        raise ValueError("business_database_password_file is invalid or unreadable")
    return SecretStr(url.set(password=password).render_as_string(hide_password=False))


class Settings(BaseSettings):
    """Application settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="INVOICE_INTELLIGENCE_",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "Invoice Intelligence"
    environment: Environment = Environment.DEVELOPMENT
    dev_tenant_id: str | None = None
    auth_mode: Literal["development", "oidc"] = "development"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_client_id: str = "invoice-intelligence-api"
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    oidc_tenant_claim: str = "tenant_id"
    oidc_reviewer_claim: str = "reviewer_id"
    oidc_jwks_cache_seconds: float = Field(default=300.0, gt=0)
    oidc_timeout_seconds: float = Field(default=5.0, gt=0)
    oidc_tls_verify: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    log_file_enabled: bool | None = None
    log_directory: Path = Path("logs")
    log_file_max_bytes: int = Field(default=20_000_000, ge=1_000_000, le=100_000_000)
    log_file_backup_count: int = Field(default=5, ge=1, le=20)
    log_file_retention_days: int = Field(default=7, ge=1, le=365)
    api_prefix: str = "/api/v1"
    docs_enabled: bool = True
    metrics_endpoint_enabled: bool = False
    file_storage_backend: Literal["local", "s3"] = "local"
    file_storage_root: Path = Path(".data/documents")
    object_storage_endpoint_url: str | None = None
    object_storage_region: str = "us-east-1"
    object_storage_access_key: SecretStr | None = None
    object_storage_secret_key: SecretStr | None = None
    object_storage_access_key_file: Path | None = None
    object_storage_secret_key_file: Path | None = None
    object_storage_originals_bucket: str = "invoice-originals"
    object_storage_rendered_bucket: str = "invoice-rendered"
    object_storage_derived_text_bucket: str = "invoice-derived-text"
    object_storage_tenant_hmac_key: SecretStr = SecretStr("development-storage-hmac-key")
    object_storage_tls_verify: bool = True
    object_storage_presign_ttl_seconds: int = Field(default=300, gt=0, le=3600)
    object_storage_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    object_storage_read_timeout_seconds: float = Field(default=30.0, gt=0)
    object_storage_max_attempts: int = Field(default=4, gt=0, le=10)
    object_storage_original_retention_days: int = Field(default=2555, gt=0)
    object_storage_rendered_retention_days: int = Field(default=7, gt=0)
    object_storage_derived_text_retention_days: int = Field(default=30, gt=0)
    storage_lifecycle_poll_interval_seconds: float = Field(default=10.0, gt=0)
    storage_lifecycle_batch_size: int = Field(default=32, gt=0, le=500)
    storage_lifecycle_lease_seconds: int = Field(default=300, gt=0)
    max_upload_bytes: int = Field(default=26_214_400, gt=0)
    max_pdf_pages: int = Field(default=20, gt=0)
    pdf_render_dpi: int = Field(default=200, gt=0)
    max_image_dimension: int = Field(default=4096, gt=0)
    max_image_pixels: int = Field(default=20_000_000, gt=0)
    max_total_rendered_pixels: int = Field(default=80_000_000, gt=0)
    image_preprocessing_enabled: bool = True
    image_preprocessing_contrast: float = Field(default=1.12, gt=0, le=2.0)
    image_preprocessing_sharpness: float = Field(default=1.18, gt=0, le=2.0)
    business_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://invoice_intelligence:change-me@localhost:5432/"
        "invoice_intelligence"
    )
    business_database_password_file: Path | None = None
    checkpoint_backend: Literal["postgres", "sqlite"] = "sqlite"
    sqlite_checkpoint_path: Path = Path(".data/checkpoints.sqlite")
    postgres_checkpoint_dsn: SecretStr | None = None
    invoice_schema_version: str = "3.0.0"
    field_semantic_base_catalog_version: str = "field-semantics-base-v3"
    field_semantic_index_enabled: bool = False
    field_semantic_index_collection_prefix: str = "invoice_field_semantics"
    field_semantic_index_alias: str = "invoice_field_semantics_current"
    field_semantic_index_projection_batch_size: int = Field(default=32, gt=0, le=500)
    index_projection_worker_enabled: bool = False
    index_projection_worker_id: str | None = None
    index_projection_worker_tenant_ids: tuple[str, ...] = ()
    index_projection_worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    index_projection_worker_batch_size: int = Field(default=32, gt=0, le=500)
    index_projection_worker_lease_seconds: float = Field(default=300.0, gt=0)
    index_projection_worker_max_attempts: int = Field(default=5, gt=0, le=100)
    index_projection_worker_backoff_base_seconds: float = Field(default=2.0, gt=0)
    index_projection_worker_backoff_max_seconds: float = Field(default=300.0, gt=0)
    index_projection_worker_mode: Literal["reviewed_examples", "field_semantics", "both"] = "both"
    index_projection_worker_verify: bool = True

    @field_validator("index_projection_worker_id", mode="before")
    @classmethod
    def normalize_index_projection_worker_id(cls, value: object) -> object:
        if value == "":
            return None
        return value
    field_semantic_binding_enabled: bool = False
    field_semantic_binding_candidate_k: int = Field(default=24, ge=2, le=500)
    field_semantic_binding_result_limit: int = Field(default=5, ge=2, le=100)
    field_semantic_binding_fusion_strategy: Literal["weighted", "rrf"] = "weighted"
    field_semantic_binding_dense_weight: float = Field(default=0.7, ge=0.0)
    field_semantic_binding_sparse_weight: float = Field(default=0.3, ge=0.0)
    field_semantic_binding_min_candidate_score: float = Field(
        default=0.45, ge=0.0, le=1.0
    )
    field_semantic_binding_acceptance_score: float = Field(
        default=0.78, ge=0.0, le=1.0
    )
    field_semantic_binding_min_score_margin: float = Field(
        default=0.12, ge=0.0, le=1.0
    )
    field_semantic_binding_min_rerank_score: float = 0.0
    field_semantic_binding_exact_weight: float = Field(default=0.30, ge=0.0)
    field_semantic_binding_retrieval_weight: float = Field(default=0.20, ge=0.0)
    field_semantic_binding_rerank_weight: float = Field(default=0.25, ge=0.0)
    field_semantic_binding_context_weight: float = Field(default=0.15, ge=0.0)
    field_semantic_binding_position_weight: float = Field(default=0.05, ge=0.0)
    field_semantic_binding_value_type_weight: float = Field(default=0.05, ge=0.0)
    field_semantic_binding_require_value_type_for_acceptance: bool = True
    field_semantic_binding_policy_version: str = "field-semantic-binding-v1"
    field_alias_learning_policy_version: str = "field-alias-learning-v1"
    field_alias_support_window_days: int = Field(default=90, gt=0)
    field_alias_min_distinct_documents: int = Field(default=2, ge=0)
    field_alias_min_distinct_templates: int = Field(default=1, ge=0)
    field_alias_min_distinct_reviewers: int = Field(default=2, ge=0)
    field_alias_global_min_distinct_tenants: int = Field(default=3, ge=2)
    field_alias_global_hash_salt: SecretStr | None = None
    field_alias_global_hash_salt_version: str = "field-alias-global-salt-v1"
    vision_prompt_version: str = "invoice-vision-extraction-v2"
    vision_prompt_max_examples_total: int = Field(default=12, gt=0, le=100)
    vision_prompt_max_examples_per_region: int = Field(default=6, gt=0, le=50)
    vision_prompt_max_correction_events: int = Field(default=6, gt=0, le=50)
    vision_prompt_max_catalog_definitions: int = Field(default=64, gt=0, le=500)
    vision_prompt_max_section_chars: int = Field(default=12_000, gt=0)
    vision_prompt_max_total_chars: int = Field(default=32_000, gt=0)
    memory_admission_policy_version: str = "memory-admission-v1"
    memory_conflict_resolution_policy_version: str = "memory-conflict-resolution-v1"
    memory_admission_reviewer_profile_version: str = "reviewer-reliability-v1"
    memory_admission_worker_id: str | None = None
    memory_admission_worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    memory_admission_worker_batch_size: int = Field(default=8, gt=0, le=1_000)
    memory_admission_worker_max_concurrency: int = Field(default=4, gt=0, le=128)
    memory_admission_worker_lease_seconds: float = Field(default=300.0, gt=0)
    memory_admission_worker_max_attempts: int = Field(default=5, gt=0, le=100)
    memory_admission_worker_backoff_base_seconds: float = Field(default=2.0, gt=0)
    memory_admission_worker_backoff_max_seconds: float = Field(default=300.0, gt=0)
    training_provider: Literal["stub", "mlflow_compatible"] = "stub"
    training_remote_base_url: str | None = None
    training_remote_bearer_token: SecretStr | None = None
    training_remote_submit_path: str = "/training/jobs"
    training_remote_job_path_template: str = "/training/jobs/{job_id}"
    training_remote_cancel_path_template: str = "/training/jobs/{job_id}/cancel"
    training_remote_timeout_seconds: float = Field(default=30.0, gt=0, le=300.0)
    training_artifact_allowed_https_hosts: tuple[str, ...] = ()
    training_artifact_file_root: Path | None = None
    training_artifact_max_bytes: int = Field(default=5_368_709_120, gt=0)
    training_worker_id: str | None = None
    training_worker_poll_interval_seconds: float = Field(default=5.0, gt=0)
    training_worker_batch_size: int = Field(default=4, gt=0, le=100)
    training_worker_lease_seconds: float = Field(default=300.0, gt=0)
    training_worker_max_attempts: int = Field(default=5, gt=0, le=100)
    training_worker_backoff_base_seconds: float = Field(default=5.0, gt=0)
    training_worker_backoff_max_seconds: float = Field(default=300.0, gt=0)
    evaluation_runner_endpoint: str | None = None
    evaluation_runner_token_file: Path | None = None
    evaluation_runner_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    evaluation_runner_max_retries: int = Field(default=2, ge=0, le=10)
    evaluation_runner_max_concurrency: int = Field(default=4, gt=0, le=64)
    evaluation_suite_timeout_seconds: float = Field(default=3600.0, gt=0)
    review_task_default_lease_seconds: int = Field(default=900, gt=0, le=86_400)
    review_task_maximum_lease_seconds: int = Field(default=3600, gt=0, le=86_400)
    memory_admission_require_distinct_second_reviewer: bool = True
    memory_admission_auto_approval_risk_levels: tuple[
        Literal["low", "standard", "high", "critical"], ...
    ] = ("low",)
    memory_admission_min_quality_scores: dict[
        Literal["low", "standard", "high", "critical"], float
    ] = Field(default_factory=_default_admission_quality_scores)
    memory_admission_min_reviewer_reliability_score: float = Field(
        default=0.70,
        ge=0.0,
        le=1.0,
    )
    memory_admission_mandatory_business_rules: tuple[str, ...] = (
        "Current visual evidence has higher authority than reviewed claims or history.",
        "Insufficient or conflicting evidence requires an attributable second review.",
        "A model recommendation cannot establish or change the invoice business truth.",
    )
    memory_admission_field_business_rules: dict[str, tuple[str, ...]] = Field(
        default_factory=dict
    )
    memory_quality_min_distinct_documents: int = Field(default=2, gt=0)
    memory_quality_min_distinct_templates: int = Field(default=1, gt=0)
    memory_quality_min_distinct_reviewers: int = Field(default=2, gt=0)
    memory_quality_max_text_length: int = Field(default=4_000, gt=0)
    memory_quality_max_control_character_ratio: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
    )
    memory_quality_max_repeated_character_run: int = Field(default=64, ge=2)
    memory_quality_field_risk_levels: dict[
        str,
        Literal["low", "standard", "high", "critical"],
    ] = Field(default_factory=dict)
    reviewed_example_fingerprint_salt: SecretStr = SecretStr(
        "development-reviewed-example-salt"
    )
    reviewed_example_retention_days: int | None = Field(default=None, gt=0)
    correction_memory_backend: Literal["postgres_pgvector", "disabled"] = (
        "postgres_pgvector"
    )
    correction_context_limit: int = Field(default=6, gt=0, le=20)
    correction_memory_min_similarity: float = Field(default=0.72, ge=0.0, le=1.0)
    correction_memory_per_scope_limit: int = Field(default=2, gt=0, le=10)
    correction_memory_max_scopes: int = Field(default=32, gt=0, le=200)
    correction_memory_vector_dimensions: Literal[1536] = 1536
    correction_embedding_model: str = "text-embedding-3-small"
    correction_embedding_timeout_seconds: float = Field(default=30.0, gt=0)
    correction_embedding_max_retries: int = Field(default=2, ge=0, le=10)
    correction_vendor_feature_fields: tuple[str, ...] = (
        "seller_name",
        "attribute_1",
    )
    correction_template_feature_fields: tuple[str, ...] = (
        "invoice_collection_type_desc",
    )
    correction_memory_redaction_policy: Literal["mask", "hash", "none"] = "mask"
    correction_memory_sensitive_field_patterns: tuple[str, ...] = (
        "tax_number$",
        "bank_account$",
        "address_phone$",
        "invoice_number$",
        "invoice_code$",
        "check_code$",
    )
    correction_memory_hash_salt: SecretStr | None = None
    correction_memory_redact_reason: bool = True
    example_index_redaction_policy: Literal["none", "mask", "hash", "drop"] = "mask"
    example_index_redaction_policy_version: str = "1"
    example_index_hash_salt: SecretStr | None = None
    example_index_tenant_policies: dict[str, TenantRedactionPolicySettings] = Field(
        default_factory=dict
    )
    example_index_projection_batch_size: int = Field(default=32, gt=0, le=500)
    example_retrieval_confirmed_correct: ExampleRetrievalCategorySettings = Field(
        default_factory=ExampleRetrievalCategorySettings
    )
    example_retrieval_corrected: ExampleRetrievalCategorySettings = Field(
        default_factory=ExampleRetrievalCategorySettings
    )
    example_retrieval_confirmed_incorrect: ExampleRetrievalCategorySettings = Field(
        default_factory=lambda: ExampleRetrievalCategorySettings(candidate_k=16, top_k=2)
    )
    example_retrieval_field_overrides: dict[
        str,
        dict[
            Literal["confirmed_correct", "corrected", "confirmed_incorrect"],
            ExampleRetrievalCategoryOverrideSettings,
        ],
    ] = Field(default_factory=dict)
    example_retrieval_prompt_version: str = "invoice-example-retrieval-v1"
    example_retrieval_policy_version: str = "hybrid-retrieval-v1"
    example_retrieval_threshold_version: str = "hybrid-thresholds-v1"
    example_retrieval_feature_fingerprint_salt: SecretStr | None = None
    example_retrieval_max_fields: int = Field(default=12, gt=0, le=200)
    milvus_enabled: bool = False
    milvus_uri: str | None = None
    milvus_token: SecretStr | None = None
    milvus_collection_prefix: str = "invoice_examples"
    milvus_alias: str = "invoice_examples_current"
    milvus_dedicated_tenant_collections: dict[str, str] = Field(default_factory=dict)
    milvus_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    milvus_operation_timeout_seconds: float = Field(default=30.0, gt=0)
    milvus_max_retries: int = Field(default=2, ge=0, le=10)
    milvus_consistency_level: Literal[
        "Strong", "Session", "Bounded", "Eventually", "Customized"
    ] = "Bounded"
    milvus_batch_size: int = Field(default=64, gt=0, le=1000)
    milvus_dense_dimension: int = Field(default=1536, gt=0)
    milvus_hnsw_m: int = Field(default=64, gt=0)
    milvus_hnsw_ef_construction: int = Field(default=100, gt=0)
    milvus_search_ef: int = Field(default=128, gt=0)
    milvus_sparse_enabled: bool = True
    milvus_bm25_function_enabled: bool = True
    milvus_sparse_metric_type: Literal["BM25", "IP"] = "BM25"
    milvus_dense_weight: float = Field(default=0.7, ge=0)
    milvus_sparse_weight: float = Field(default=0.3, ge=0)
    milvus_ranker: Literal["weighted", "rrf"] = "weighted"
    milvus_max_text_length: int = Field(default=65_535, gt=0)
    validation_accept_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    validation_reject_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    validation_min_clarity: float = Field(default=0.45, ge=0.0, le=1.0)
    validation_min_width: int = Field(default=800, gt=0)
    validation_min_height: int = Field(default=600, gt=0)
    validation_ocr_match_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    validation_amount_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)
    validation_field_overrides: dict[str, ValidationFieldOverrideSettings] = Field(
        default_factory=dict
    )
    ocr_enabled: bool = False
    ocr_base_url: str = "http://127.0.0.1:8188"
    ocr_endpoint_path: str = "/ocr"
    ocr_provider_version: str = "paddlex-3.7.2"
    ocr_model_version: str = "PP-OCRv6_small_det+PP-OCRv6_small_rec"
    ocr_pipeline_config_version: str = "ppocrv6-small-v1"
    ocr_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    ocr_read_timeout_seconds: float = Field(default=30.0, gt=0)
    ocr_write_timeout_seconds: float = Field(default=30.0, gt=0)
    ocr_pool_timeout_seconds: float = Field(default=5.0, gt=0)
    ocr_max_retries: int = Field(default=2, ge=0, le=10)
    ocr_max_concurrency: int = Field(default=4, gt=0, le=100)
    ocr_requests_per_minute: int = Field(default=60, gt=0)
    ocr_backoff_base_seconds: float = Field(default=0.5, gt=0)
    ocr_backoff_max_seconds: float = Field(default=8.0, gt=0)
    ocr_circuit_failure_threshold: int = Field(default=5, gt=0)
    ocr_circuit_recovery_seconds: float = Field(default=30.0, gt=0)
    ocr_max_input_bytes: int = Field(default=26_214_400, gt=0)
    ocr_remote_providers: tuple[PaddleXCompatibleOCRProviderSettings, ...] = ()
    ocr_comparison_policy_version: str = "multi-source-ocr-comparison-v1"
    ocr_comparison_provider_score_thresholds: dict[str, float] = Field(
        default_factory=lambda: {"paddlex_ocr_http": 0.0}
    )
    ocr_comparison_default_provider_score_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
    )
    ocr_comparison_covered_field_paths: tuple[str, ...] = ()
    ocr_comparison_default_field_risk_level: Literal[
        "low", "standard", "high", "critical"
    ] = "standard"
    ocr_comparison_field_risk_levels: dict[
        str,
        Literal["low", "standard", "high", "critical"],
    ] = Field(default_factory=dict)
    ocr_comparison_review_risk_levels: tuple[
        Literal["low", "standard", "high", "critical"], ...
    ] = ("high", "critical")
    ocr_comparison_amount_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)
    ocr_comparison_max_context_observations: int = Field(default=6, gt=0, le=50)
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-5.6"
    openai_timeout_seconds: float = Field(default=60.0, gt=0)
    openai_max_retries: int = Field(default=2, ge=0, le=10)
    openai_vision_schema_max_retries: int = Field(default=2, ge=0, le=3)
    openai_max_concurrency: int = Field(default=8, gt=0, le=128)
    openai_requests_per_minute: int = Field(default=60, gt=0)
    openai_circuit_failure_threshold: int = Field(default=5, gt=0)
    openai_circuit_recovery_seconds: float = Field(default=30.0, gt=0)
    openai_image_detail: Literal["auto", "high", "low", "original"] = "original"
    vision_provider: Literal["openai", "qwen", "disabled"] = "openai"
    embedding_provider: Literal["openai", "qwen", "disabled"] = "openai"
    reranking_provider: Literal["qwen", "disabled"] = "disabled"
    query_rewrite_provider: Literal["qwen", "disabled"] = "disabled"
    memory_quality_assessment_provider: Literal["qwen", "disabled"] = "disabled"
    qwen_api_key: SecretStr | None = None
    qwen_compatible_base_url: str | None = None
    qwen_rerank_base_url: str | None = None
    qwen_vision_model: str = "qwen3.8-max"
    qwen_embedding_model: str = "qwen3.7-text-embedding"
    qwen_embedding_dimensions: int = Field(default=1536, gt=0)
    qwen_embedding_batch_size: int = Field(default=10, gt=0, le=10)
    qwen_rerank_model: str = "qwen3-rerank"
    qwen_rerank_instruct: str = "Retrieve semantically similar text."
    qwen_rerank_max_documents: int = Field(default=500, gt=0, le=500)
    qwen_query_rewrite_model: str = "qwen3.8-flash"
    qwen_query_rewrite_prompt_version: str = "qwen-query-rewrite-v1"
    qwen_query_rewrite_max_expansion_terms: int = Field(default=8, gt=0, le=32)
    qwen_memory_quality_assessment_model: str = "qwen3.8-max"
    qwen_memory_quality_assessment_prompt_version: str = (
        "qwen-memory-quality-assessment-v1"
    )
    qwen_timeout_seconds: float = Field(default=60.0, gt=0)
    qwen_max_retries: int = Field(default=2, ge=0, le=10)
    qwen_vision_schema_max_retries: int = Field(default=2, ge=0, le=3)
    qwen_max_concurrency: int = Field(default=8, gt=0, le=100)
    qwen_requests_per_minute: int = Field(default=60, gt=0)
    qwen_backoff_base_seconds: float = Field(default=0.5, gt=0)
    qwen_backoff_max_seconds: float = Field(default=8.0, gt=0)
    qwen_circuit_failure_threshold: int = Field(default=5, gt=0)
    qwen_circuit_recovery_seconds: float = Field(default=30.0, gt=0)
    qwen_allow_unredacted_vision_images: bool = False
    qwen_audit_enabled: bool = True
    qwen_model_costs_per_million: dict[str, QwenModelCostSettings] = Field(
        default_factory=dict
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """Normalize string log levels before validation."""

        return value.upper() if isinstance(value, str) else value

    @field_validator("ocr_base_url")
    @classmethod
    def validate_ocr_base_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ocr_base_url must be an absolute HTTP(S) URL")
        return normalized.rstrip("/")

    @field_validator("ocr_endpoint_path")
    @classmethod
    def validate_ocr_endpoint_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("ocr_endpoint_path must start with '/'")
        return normalized

    @model_validator(mode="after")
    def validate_ocr_backoff(self) -> "Settings":
        if self.ocr_backoff_max_seconds < self.ocr_backoff_base_seconds:
            raise ValueError("ocr_backoff_max_seconds must not be lower than base backoff")
        names = tuple(
            provider.provider_name
            for provider in self.ocr_remote_providers
            if provider.enabled
        )
        if len(names) != len(set(names)) or "paddlex_ocr_http" in names:
            raise ValueError(
                "Enabled remote OCR provider names must be unique and cannot use "
                "the reserved local name paddlex_ocr_http"
            )
        return self

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        """Require a normalized absolute API path prefix."""

        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("api_prefix must start with '/'")
        normalized = normalized.rstrip("/")
        if not normalized:
            raise ValueError("api_prefix must not be the root path")
        return normalized

    @field_validator(
        "object_storage_access_key",
        "object_storage_secret_key",
        "object_storage_access_key_file",
        "object_storage_secret_key_file",
        mode="before",
    )
    @classmethod
    def normalize_optional_storage_secret(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def validate_authentication_settings(self) -> "Settings":
        if self.environment is Environment.PRODUCTION:
            if self.dev_tenant_id is not None:
                raise ValueError("dev_tenant_id is forbidden in production")
            if self.auth_mode != "oidc":
                raise ValueError("production requires auth_mode=oidc")
        if self.auth_mode == "oidc":
            required = {
                "oidc_issuer": self.oidc_issuer,
                "oidc_audience": self.oidc_audience,
                "oidc_jwks_url": self.oidc_jwks_url,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError("OIDC settings are missing: " + ", ".join(missing))
        if self.environment is Environment.PRODUCTION:
            assert self.oidc_issuer is not None
            assert self.oidc_jwks_url is not None
            if urlsplit(self.oidc_issuer).scheme != "https":
                raise ValueError("production OIDC issuer must use HTTPS")
            if urlsplit(self.oidc_jwks_url).scheme != "https":
                raise ValueError("production OIDC JWKS URL must use HTTPS")
            if not self.oidc_tls_verify:
                raise ValueError("production OIDC TLS verification cannot be disabled")
        if not self.oidc_algorithms or any(not item.strip() for item in self.oidc_algorithms):
            raise ValueError("oidc_algorithms must contain normalized algorithms")
        return self

    @field_validator(
        "openai_model",
        "correction_embedding_model",
        "invoice_schema_version",
        "field_semantic_base_catalog_version",
        "field_semantic_index_collection_prefix",
        "field_semantic_index_alias",
        "field_semantic_binding_policy_version",
        "field_alias_learning_policy_version",
        "field_alias_global_hash_salt_version",
        "vision_prompt_version",
        "ocr_provider_version",
        "ocr_model_version",
        "ocr_pipeline_config_version",
        "ocr_comparison_policy_version",
        "memory_admission_policy_version",
        "memory_conflict_resolution_policy_version",
        "memory_admission_reviewer_profile_version",
        "example_index_redaction_policy_version",
        "example_retrieval_prompt_version",
        "example_retrieval_policy_version",
        "example_retrieval_threshold_version",
        "milvus_collection_prefix",
        "milvus_alias",
        "qwen_vision_model",
        "qwen_embedding_model",
        "qwen_rerank_model",
        "qwen_rerank_instruct",
        "qwen_query_rewrite_model",
        "qwen_query_rewrite_prompt_version",
        "qwen_memory_quality_assessment_model",
        "qwen_memory_quality_assessment_prompt_version",
    )
    @classmethod
    def validate_named_setting(cls, value: str) -> str:
        """Reject empty model identifiers and Schema versions."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("Model identifiers and Schema versions must not be empty")
        return normalized

    @field_validator("ocr_comparison_provider_score_thresholds")
    @classmethod
    def normalize_ocr_provider_score_thresholds(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for provider, threshold in value.items():
            name = provider.strip()
            if not name or name != provider:
                raise ValueError("OCR provider score threshold keys must be normalized")
            if not 0.0 <= threshold <= 1.0:
                raise ValueError("OCR provider score thresholds must be between zero and one")
            normalized[name] = threshold
        return normalized

    @field_validator("ocr_comparison_covered_field_paths")
    @classmethod
    def normalize_ocr_covered_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(path.strip() for path in value)
        if any(not path for path in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("OCR covered field paths must be non-empty and unique")
        return normalized

    @field_validator("ocr_comparison_field_risk_levels")
    @classmethod
    def normalize_ocr_field_risks(
        cls,
        value: dict[str, Literal["low", "standard", "high", "critical"]],
    ) -> dict[str, Literal["low", "standard", "high", "critical"]]:
        normalized: dict[str, Literal["low", "standard", "high", "critical"]] = {}
        for field_path, risk in value.items():
            path = field_path.strip()
            if not path or path != field_path:
                raise ValueError("OCR field risk keys must be normalized")
            normalized[path] = risk
        return normalized

    @field_validator("ocr_comparison_review_risk_levels")
    @classmethod
    def validate_ocr_review_risks(
        cls,
        value: tuple[Literal["low", "standard", "high", "critical"], ...],
    ) -> tuple[Literal["low", "standard", "high", "critical"], ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("OCR review risk levels must be non-empty and unique")
        return value

    @field_validator(
        "openai_api_key",
        "postgres_checkpoint_dsn",
        "correction_memory_hash_salt",
        "example_index_hash_salt",
        "example_retrieval_feature_fingerprint_salt",
        "milvus_token",
        "qwen_api_key",
        "reviewed_example_fingerprint_salt",
        "field_alias_global_hash_salt",
        mode="before",
    )
    @classmethod
    def normalize_empty_secret(cls, value: object) -> object:
        """Treat an empty secret placeholder as unconfigured."""

        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("milvus_uri", mode="before")
    @classmethod
    def normalize_milvus_uri(cls, value: object) -> object:
        """Treat an empty Milvus URI as an unconfigured optional adapter."""

        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator(
        "qwen_compatible_base_url",
        "qwen_rerank_base_url",
        mode="before",
    )
    @classmethod
    def normalize_qwen_url(cls, value: object) -> object:
        """Treat blank Qwen endpoints as unconfigured and require HTTPS when set."""

        if not isinstance(value, str):
            return value
        normalized = value.strip().rstrip("/")
        if not normalized:
            return None
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Qwen endpoints must use an absolute HTTPS URL")
        return normalized

    @field_validator("business_database_url", mode="before")
    @classmethod
    def validate_business_database_url(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("business_database_url must not be empty")
        normalized = raw.strip()
        if not normalized.startswith(
            ("sqlite+pysqlite:///", "postgresql+psycopg://")
        ):
            raise ValueError(
                "business_database_url must use sqlite+pysqlite or postgresql+psycopg"
            )
        if normalized.endswith(":memory:"):
            raise ValueError("business_database_url must use durable storage")
        return normalized

    @field_validator("business_database_password_file", mode="before")
    @classmethod
    def normalize_business_database_password_file(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def resolved_business_database_url(self) -> SecretStr:
        return _resolve_business_database_url(
            self.business_database_url,
            self.business_database_password_file,
            self.environment,
        )

    @field_validator("validation_field_overrides")
    @classmethod
    def normalize_validation_field_overrides(
        cls,
        value: dict[str, ValidationFieldOverrideSettings],
    ) -> dict[str, ValidationFieldOverrideSettings]:
        normalized: dict[str, ValidationFieldOverrideSettings] = {}
        for field_path, override in value.items():
            path = field_path.strip()
            if not path:
                raise ValueError("validation_field_overrides cannot contain an empty field path")
            if path in normalized:
                raise ValueError(f"Duplicate validation field override: {path}")
            normalized[path] = override
        return normalized

    @field_validator("memory_quality_field_risk_levels")
    @classmethod
    def normalize_memory_quality_field_risks(
        cls,
        value: dict[str, Literal["low", "standard", "high", "critical"]],
    ) -> dict[str, Literal["low", "standard", "high", "critical"]]:
        normalized: dict[
            str,
            Literal["low", "standard", "high", "critical"],
        ] = {}
        for field_path, risk in value.items():
            path = field_path.strip()
            if not path or path != field_path:
                raise ValueError(
                    "memory_quality_field_risk_levels keys must be normalized field paths"
                )
            if path in normalized:
                raise ValueError(f"Duplicate memory quality field risk: {path}")
            normalized[path] = risk
        return normalized

    @field_validator("memory_admission_auto_approval_risk_levels")
    @classmethod
    def normalize_memory_admission_risk_levels(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError(
                "memory_admission_auto_approval_risk_levels must be non-empty and unique"
            )
        return value

    @field_validator("memory_admission_worker_id", mode="before")
    @classmethod
    def normalize_memory_admission_worker_id(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            return None
        if normalized != value or len(normalized) > 128:
            raise ValueError("memory_admission_worker_id must be normalized and <= 128 chars")
        return normalized

    @model_validator(mode="after")
    def validate_memory_admission_worker_settings(self) -> "Settings":
        if (
            self.memory_admission_worker_max_concurrency
            > self.memory_admission_worker_batch_size
        ):
            raise ValueError("Memory admission Worker concurrency exceeds batch size")
        if (
            self.memory_admission_worker_backoff_max_seconds
            < self.memory_admission_worker_backoff_base_seconds
        ):
            raise ValueError("Memory admission Worker maximum backoff is below its base")
        return self

    @model_validator(mode="after")
    def validate_index_projection_worker_settings(self) -> "Settings":
        worker_id = self.index_projection_worker_id
        if worker_id is not None and (
            not worker_id or worker_id != worker_id.strip() or len(worker_id) > 128
        ):
            raise ValueError("index_projection_worker_id must be normalized and <= 128 chars")
        if self.index_projection_worker_enabled and (
            not worker_id or not self.index_projection_worker_tenant_ids
        ):
            raise ValueError("Enabled index projection worker requires ID and tenant scope")
        if (
            self.index_projection_worker_backoff_max_seconds
            < self.index_projection_worker_backoff_base_seconds
        ):
            raise ValueError("Index projection maximum backoff is below its base")
        return self

    @model_validator(mode="after")
    def validate_review_task_settings(self) -> "Settings":
        if self.review_task_maximum_lease_seconds < self.review_task_default_lease_seconds:
            raise ValueError(
                "review_task_maximum_lease_seconds cannot be lower than the default"
            )
        return self

    @model_validator(mode="after")
    def validate_vision_prompt_budget(self) -> "Settings":
        if self.vision_prompt_max_total_chars < self.vision_prompt_max_section_chars:
            raise ValueError(
                "vision_prompt_max_total_chars must cover one Prompt section"
            )
        return self

    @field_validator("memory_admission_min_quality_scores")
    @classmethod
    def validate_memory_admission_quality_scores(
        cls,
        value: dict[str, float],
    ) -> dict[str, float]:
        required = {"low", "standard", "high", "critical"}
        if set(value) != required:
            raise ValueError(
                "memory_admission_min_quality_scores must define every field risk level"
            )
        if any(not 0.0 <= score <= 1.0 for score in value.values()):
            raise ValueError("Memory admission quality scores must be between zero and one")
        return value

    @field_validator(
        "memory_admission_mandatory_business_rules",
        "memory_admission_field_business_rules",
    )
    @classmethod
    def normalize_memory_admission_rules(cls, value: object) -> object:
        if isinstance(value, tuple):
            normalized = tuple(rule.strip() for rule in value)
            if not normalized or any(not rule for rule in normalized):
                raise ValueError("Memory admission business rules must not be blank")
            if len(normalized) != len(set(normalized)):
                raise ValueError("Memory admission business rules must be unique")
            return normalized
        if isinstance(value, dict):
            normalized_mapping: dict[str, tuple[str, ...]] = {}
            for field_path, rules in value.items():
                path = field_path.strip()
                normalized_rules = tuple(rule.strip() for rule in rules)
                if not path or path != field_path or not normalized_rules:
                    raise ValueError(
                        "Field business rules require normalized paths and non-empty rules"
                    )
                if any(not rule for rule in normalized_rules) or len(
                    normalized_rules
                ) != len(set(normalized_rules)):
                    raise ValueError("Field business rules must be non-empty and unique")
                normalized_mapping[path] = normalized_rules
            return normalized_mapping
        raise ValueError("Memory admission business rules have an invalid shape")

    @field_validator("example_retrieval_field_overrides")
    @classmethod
    def normalize_example_retrieval_field_overrides(
        cls,
        value: dict[
            str,
            dict[
                Literal["confirmed_correct", "corrected", "confirmed_incorrect"],
                ExampleRetrievalCategoryOverrideSettings,
            ],
        ],
    ) -> dict[
        str,
        dict[
            Literal["confirmed_correct", "corrected", "confirmed_incorrect"],
            ExampleRetrievalCategoryOverrideSettings,
        ],
    ]:
        normalized = {}
        for field_path, overrides in value.items():
            path = field_path.strip()
            if not path or path != field_path:
                raise ValueError(
                    "example_retrieval_field_overrides keys must be normalized field paths"
                )
            if not overrides:
                raise ValueError("example retrieval field overrides must not be empty")
            normalized[path] = overrides
        return normalized

    @field_validator("qwen_model_costs_per_million")
    @classmethod
    def normalize_qwen_model_costs(
        cls,
        value: dict[str, QwenModelCostSettings],
    ) -> dict[str, QwenModelCostSettings]:
        normalized: dict[str, QwenModelCostSettings] = {}
        for model, rates in value.items():
            model_name = model.strip()
            if not model_name or model_name != model:
                raise ValueError("Qwen model cost keys must be normalized model names")
            normalized[model_name] = rates
        return normalized

    @field_validator(
        "correction_vendor_feature_fields",
        "correction_template_feature_fields",
        "correction_memory_sensitive_field_patterns",
    )
    @classmethod
    def normalize_correction_memory_lists(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("Correction-memory field and pattern lists cannot contain blanks")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Correction-memory field and pattern lists cannot contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_persistence_separation(self) -> "Settings":
        """Keep workflow checkpoints separate from the business database."""

        buckets = {
            self.object_storage_originals_bucket,
            self.object_storage_rendered_bucket,
            self.object_storage_derived_text_bucket,
        }
        if len(buckets) != 3 or any(not item.strip() for item in buckets):
            raise ValueError("Object storage buckets must be three distinct non-empty names")
        if self.object_storage_access_key is not None and self.object_storage_access_key_file:
            raise ValueError("Set object storage access key or access key file, not both")
        if self.object_storage_secret_key is not None and self.object_storage_secret_key_file:
            raise ValueError("Set object storage secret key or secret key file, not both")
        if self.file_storage_backend == "s3":
            if self.object_storage_endpoint_url is None:
                raise ValueError("S3 storage requires object_storage_endpoint_url")
            parsed_storage_endpoint = urlsplit(self.object_storage_endpoint_url)
            if parsed_storage_endpoint.scheme not in {"http", "https"}:
                raise ValueError("Object storage endpoint must use HTTP(S)")
        if self.environment is Environment.PRODUCTION:
            if self.file_storage_backend == "local":
                raise ValueError("Production forbids LocalFileStorage")
            assert self.object_storage_endpoint_url is not None
            if urlsplit(self.object_storage_endpoint_url).scheme != "https":
                raise ValueError("Production object storage endpoint must use HTTPS")
            if not self.object_storage_tls_verify:
                raise ValueError("Production object storage TLS verification cannot be disabled")
            if (
                self.object_storage_tenant_hmac_key.get_secret_value()
                == "development-storage-hmac-key"
            ):
                raise ValueError("Production requires a non-default object storage HMAC key")
        if self.validation_reject_threshold >= self.validation_accept_threshold:
            raise ValueError(
                "validation_reject_threshold must be lower than validation_accept_threshold"
            )
        if (
            self.environment is Environment.PRODUCTION
            and self.reviewed_example_fingerprint_salt.get_secret_value()
            == "development-reviewed-example-salt"
        ):
            raise ValueError(
                "Production requires a non-default reviewed_example_fingerprint_salt"
            )
        business_url = self.business_database_url.get_secret_value()
        if (
            self.correction_memory_backend == "postgres_pgvector"
            and not business_url.startswith("postgresql+psycopg://")
        ):
            raise ValueError(
                "postgres_pgvector correction memory requires a PostgreSQL business database"
            )
        if (
            self.correction_memory_redaction_policy == "hash"
            and self.correction_memory_hash_salt is None
        ):
            raise ValueError("correction_memory_hash_salt is required for hash redaction")
        if (
            self.example_index_redaction_policy == "hash"
            and self.example_index_hash_salt is None
        ):
            raise ValueError("example_index_hash_salt is required for hash redaction")
        for tenant_id, policy in self.example_index_tenant_policies.items():
            if not tenant_id.strip():
                raise ValueError("example_index_tenant_policies cannot contain a blank tenant")
        if self.milvus_enabled and (
            self.example_index_redaction_policy == "none"
            or any(
                policy.strategy == "none"
                for policy in self.example_index_tenant_policies.values()
            )
        ):
            raise ValueError("Milvus reviewed-example projections require redaction")
        for tenant_id, collection in self.milvus_dedicated_tenant_collections.items():
            if not tenant_id.strip() or not collection.strip():
                raise ValueError(
                    "milvus_dedicated_tenant_collections cannot contain blank keys or values"
                )
        if self.milvus_enabled and self.milvus_uri is None:
            raise ValueError("milvus_uri is required when milvus_enabled is true")
        if self.field_semantic_index_enabled and not self.milvus_enabled:
            raise ValueError("field_semantic_index_enabled requires milvus_enabled")
        if self.field_semantic_index_enabled and not self.milvus_sparse_enabled:
            raise ValueError("Field semantic indexing requires Milvus sparse retrieval")
        if (
            self.field_semantic_index_enabled
            and self.embedding_provider == "disabled"
        ):
            raise ValueError("Field semantic indexing requires dense embeddings")
        if self.field_semantic_index_collection_prefix == self.milvus_collection_prefix:
            raise ValueError(
                "Field semantic and reviewed-example collection prefixes must differ"
            )
        if self.field_semantic_index_alias == self.milvus_alias:
            raise ValueError(
                "Field semantic and reviewed-example Milvus aliases must differ"
            )
        if self.field_semantic_binding_enabled and not self.field_semantic_index_enabled:
            raise ValueError(
                "field_semantic_binding_enabled requires field_semantic_index_enabled"
            )
        if self.field_semantic_binding_enabled and self.reranking_provider == "disabled":
            raise ValueError("Field semantic binding requires a reranking provider")
        if (
            self.field_semantic_binding_result_limit
            > self.field_semantic_binding_candidate_k
        ):
            raise ValueError("Field semantic binding result limit exceeds candidate_k")
        if (
            self.field_semantic_binding_min_candidate_score
            > self.field_semantic_binding_acceptance_score
        ):
            raise ValueError(
                "Field semantic binding candidate score exceeds acceptance score"
            )
        if (
            self.field_semantic_binding_dense_weight
            + self.field_semantic_binding_sparse_weight
            <= 0
        ):
            raise ValueError("Field semantic binding requires a retrieval weight")
        binding_component_weights = (
            self.field_semantic_binding_exact_weight,
            self.field_semantic_binding_retrieval_weight,
            self.field_semantic_binding_rerank_weight,
            self.field_semantic_binding_context_weight,
            self.field_semantic_binding_position_weight,
            self.field_semantic_binding_value_type_weight,
        )
        if sum(binding_component_weights) <= 0:
            raise ValueError("Field semantic binding requires a component weight")
        if (
            self.field_semantic_binding_enabled
            and self.reranking_provider == "qwen"
            and self.field_semantic_binding_candidate_k
            > self.qwen_rerank_max_documents
        ):
            raise ValueError("Field semantic binding candidate_k exceeds Qwen rerank limit")
        if (
            self.milvus_sparse_enabled
            and self.milvus_bm25_function_enabled
            and self.milvus_sparse_metric_type != "BM25"
        ):
            raise ValueError("Milvus BM25 Function requires BM25 sparse_metric_type")
        if self.milvus_dense_weight + self.milvus_sparse_weight <= 0:
            raise ValueError("At least one Milvus fusion weight must be greater than zero")
        category_defaults = {
            "confirmed_correct": self.example_retrieval_confirmed_correct,
            "corrected": self.example_retrieval_corrected,
            "confirmed_incorrect": self.example_retrieval_confirmed_incorrect,
        }
        for field_path, overrides in self.example_retrieval_field_overrides.items():
            for label, override in overrides.items():
                default = category_defaults[label]
                candidate_k = override.candidate_k or default.candidate_k
                top_k = override.top_k or default.top_k
                if top_k > candidate_k:
                    raise ValueError(
                        f"example retrieval top_k exceeds candidate_k for {field_path}:{label}"
                    )
                dense_weight = (
                    override.dense_weight
                    if override.dense_weight is not None
                    else (
                        default.dense_weight
                        if default.dense_weight is not None
                        else self.milvus_dense_weight
                    )
                )
                sparse_weight = (
                    override.sparse_weight
                    if override.sparse_weight is not None
                    else (
                        default.sparse_weight
                        if default.sparse_weight is not None
                        else self.milvus_sparse_weight
                    )
                )
                if dense_weight + sparse_weight <= 0:
                    raise ValueError(
                        "At least one retrieval weight is required for "
                        f"{field_path}:{label}"
                    )
        for category_label, category in category_defaults.items():
            dense_weight = (
                category.dense_weight
                if category.dense_weight is not None
                else self.milvus_dense_weight
            )
            sparse_weight = (
                category.sparse_weight
                if category.sparse_weight is not None
                else self.milvus_sparse_weight
            )
            if dense_weight + sparse_weight <= 0:
                raise ValueError(
                    f"At least one retrieval weight is required for {category_label}"
                )
        qwen_selected = (
            self.vision_provider == "qwen"
            or self.embedding_provider == "qwen"
            or self.reranking_provider == "qwen"
            or self.query_rewrite_provider == "qwen"
            or self.memory_quality_assessment_provider == "qwen"
        )
        if qwen_selected and self.qwen_api_key is not None:
            compatible_selected = (
                self.vision_provider == "qwen"
                or self.embedding_provider == "qwen"
                or self.query_rewrite_provider == "qwen"
                or self.memory_quality_assessment_provider == "qwen"
            )
            if compatible_selected and self.qwen_compatible_base_url is None:
                raise ValueError(
                    "Qwen compatible endpoint is required for vision, embedding, rewrite, "
                    "and memory assessment"
                )
            if self.reranking_provider == "qwen" and self.qwen_rerank_base_url is None:
                raise ValueError("Qwen rerank endpoint is required for reranking")
        if self.qwen_backoff_max_seconds < self.qwen_backoff_base_seconds:
            raise ValueError(
                "qwen_backoff_max_seconds must be greater than or equal to the base"
            )
        if self.embedding_provider == "qwen":
            if self.qwen_embedding_dimensions != self.correction_memory_vector_dimensions:
                raise ValueError(
                    "Qwen dimensions must match the correction-memory vector dimension"
                )
            if (
                self.milvus_enabled
                and self.qwen_embedding_dimensions != self.milvus_dense_dimension
            ):
                raise ValueError("Qwen dimensions must match the Milvus dense dimension")
        if self.environment is Environment.PRODUCTION:
            if not business_url.startswith("postgresql+psycopg://"):
                raise ValueError("Production business persistence must use PostgreSQL")
            if not self.milvus_enabled:
                raise ValueError("Production reviewed-example retrieval requires Milvus")
            if self.embedding_provider == "disabled":
                raise ValueError("Production Milvus retrieval requires dense embeddings")
            if self.reranking_provider == "disabled":
                raise ValueError("Production Milvus retrieval requires reranking")
        if self.checkpoint_backend == "postgres" and self.postgres_checkpoint_dsn is None:
            raise ValueError("postgres_checkpoint_dsn is required for PostgreSQL checkpoints")
        if self.checkpoint_backend == "sqlite":
            if business_url.startswith("sqlite+pysqlite:///"):
                raw_path = business_url.removeprefix("sqlite+pysqlite:///")
                business_path = Path(raw_path).expanduser().resolve()
            else:
                business_path = None
            checkpoint_path = self.sqlite_checkpoint_path.expanduser().resolve()
            file_storage_root = self.file_storage_root.expanduser().resolve()
            if business_path is not None and business_path == checkpoint_path:
                raise ValueError(
                    "sqlite_checkpoint_path must differ from business_database_url"
                )
            if checkpoint_path.is_relative_to(file_storage_root):
                raise ValueError(
                    "SQLite checkpoint storage must be outside file_storage_root"
                )
            if business_path is not None and business_path.is_relative_to(file_storage_root):
                raise ValueError(
                    "SQLite business storage must be outside file_storage_root"
                )
        if self.checkpoint_backend == "postgres" and self.postgres_checkpoint_dsn is not None:
            business_dsn = self.business_database_url.get_secret_value()
            checkpoint_dsn = self.postgres_checkpoint_dsn.get_secret_value()
            if _postgres_database_identity(business_dsn) == _postgres_database_identity(
                checkpoint_dsn
            ):
                raise ValueError(
                    "PostgreSQL checkpoint storage must differ from the business database"
                )
        self.resolved_business_database_url
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide immutable settings instance."""

    return Settings()
