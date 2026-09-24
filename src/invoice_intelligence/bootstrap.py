"""Application composition root."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from inspect import isawaitable
from pathlib import Path
from typing import cast

import boto3  # type: ignore[import-untyped]
from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]
from pydantic import SecretStr
from sqlalchemy import Engine, text

from invoice_intelligence.application.ports.admission import (
    MemoryQualityAssessmentProvider,
)
from invoice_intelligence.application.ports.auth import AuthContextProvider, SecurityAuditSink
from invoice_intelligence.application.ports.capabilities import ProviderCapability
from invoice_intelligence.application.ports.examples import (
    DenseEmbeddingProvider,
    ExampleIndexStore,
    QueryRewriteProvider,
    RerankingProvider,
    SparseEmbeddingProvider,
    TenantRedactionPolicy,
)
from invoice_intelligence.application.ports.field_semantics import (
    FieldSemanticIndexStore,
    FieldSemanticRerankingProvider,
)
from invoice_intelligence.application.ports.file_storage import DownloadAccessIssuer, FileStorage
from invoice_intelligence.application.ports.prompt_registry import VisionPromptRegistry
from invoice_intelligence.application.ports.training_registry import (
    TrainingArtifactReader,
    TrainingProvider,
)
from invoice_intelligence.application.ports.validation import (
    OCRValidationProvider,
    RawOCRProvider,
)
from invoice_intelligence.application.ports.vision_extraction import (
    VisionExtractionProvider,
)
from invoice_intelligence.application.services.accounting import AccountingService
from invoice_intelligence.application.services.authorization import AuthorizationPolicy
from invoice_intelligence.application.services.correction_memory import CorrectionMemoryService
from invoice_intelligence.application.services.document_access import DocumentAccessService
from invoice_intelligence.application.services.document_artifacts import DocumentArtifactService
from invoice_intelligence.application.services.document_ingestion import (
    DocumentIngestionService,
)
from invoice_intelligence.application.services.evaluation_execution import (
    ALL_EVALUATION_VARIANTS,
    ConfiguredEvaluationVariantExecutor,
)
from invoice_intelligence.application.services.evaluation_jobs import EvaluationJobService
from invoice_intelligence.application.services.example_index_projection import (
    ExampleIndexProjectionService,
)
from invoice_intelligence.application.services.example_retrieval import (
    CategoryRetrievalPolicy,
    HybridExampleRetrievalService,
    HybridRetrievalPolicy,
)
from invoice_intelligence.application.services.extraction_validation import (
    EvidenceBasedExtractionValidator,
    ValidationPolicy,
    ValidationThresholdOverride,
    ValidationThresholds,
)
from invoice_intelligence.application.services.extraction_workflow import (
    ExtractionWorkflowService,
)
from invoice_intelligence.application.services.field_alias_learning import (
    FieldAliasLearningPolicy,
    FieldAliasLearningService,
)
from invoice_intelligence.application.services.field_semantic_binding import (
    FieldSemanticBindingPolicy,
    FieldSemanticBindingService,
)
from invoice_intelligence.application.services.field_semantic_catalog import (
    FieldSemanticCatalog,
)
from invoice_intelligence.application.services.field_semantic_index_projection import (
    FieldSemanticIndexProjectionService,
)
from invoice_intelligence.application.services.memory_admission import (
    DeterministicMemoryAdmissionPolicy,
    MemoryAdmissionPolicyConfig,
    MemoryAdmissionService,
)
from invoice_intelligence.application.services.memory_governance import (
    MemoryGovernanceService,
)
from invoice_intelligence.application.services.memory_quality_validation import (
    DeterministicMemoryQualityValidator,
    FieldRiskLevel,
    MemoryQualityValidationPolicy,
)
from invoice_intelligence.application.services.multi_source_ocr_comparison import (
    DeterministicMultiSourceOCRComparisonService,
    OCRComparisonPolicy,
    OCRComparisonRiskLevel,
)
from invoice_intelligence.application.services.offline_evaluation import (
    OfflineEvaluationPolicy,
    OfflineEvaluationService,
)
from invoice_intelligence.application.services.promotion_candidates import (
    PromotionCandidateService,
    PromotionPolicy,
)
from invoice_intelligence.application.services.reviewed_example_generation import (
    ReviewedExampleGenerationService,
)
from invoice_intelligence.application.services.training_jobs import TrainingJobService
from invoice_intelligence.application.services.transaction_analysis import (
    TransactionAnalysisService,
)
from invoice_intelligence.application.services.transaction_rules import MockTransactionRuleEngine
from invoice_intelligence.application.services.vision_extraction import (
    VisionExtractionService,
)
from invoice_intelligence.config.settings import Settings, get_settings
from invoice_intelligence.domain.document import DocumentProcessingLimits
from invoice_intelligence.domain.examples import (
    ExampleLabelType,
    ModelVersion,
    PromptVersion,
    RetrievalPolicyVersion,
)
from invoice_intelligence.domain.extraction import PromptContextBudget
from invoice_intelligence.domain.field_semantics import FieldSemanticCatalogVersion
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.storage import ObjectKind
from invoice_intelligence.infrastructure.accounting.mock import (
    MockAccountingPostingProvider,
    MockExchangeRateProvider,
)
from invoice_intelligence.infrastructure.auth.audit import SQLAlchemySecurityAuditSink
from invoice_intelligence.infrastructure.auth.oidc import OIDCJWTAuthContextProvider
from invoice_intelligence.infrastructure.checkpointing.factory import open_checkpointer
from invoice_intelligence.infrastructure.corrections.pydantic import (
    PydanticHumanCorrectionApplier,
)
from invoice_intelligence.infrastructure.corrections.redaction import (
    PatternSensitiveDataRedactor,
    TenantExampleRedactor,
)
from invoice_intelligence.infrastructure.corrections.scopes import (
    PydanticCorrectionScopeResolver,
)
from invoice_intelligence.infrastructure.documents.processor import (
    PillowMuPdfDocumentProcessor,
)
from invoice_intelligence.infrastructure.documents.quality import PillowImageQualityAnalyzer
from invoice_intelligence.infrastructure.embeddings.openai import OpenAIEmbeddingProvider
from invoice_intelligence.infrastructure.evaluation.http_runner import (
    IsolatedEvaluationRunnerConfig,
    IsolatedHTTPEvaluationVariantRunner,
)
from invoice_intelligence.infrastructure.indexing.integrity import (
    SQLAlchemyMilvusIndexIntegrityVerifier,
)
from invoice_intelligence.infrastructure.indexing.milvus_examples import (
    DefaultMilvusCollectionResolver,
    MilvusExampleIndexStore,
)
from invoice_intelligence.infrastructure.indexing.milvus_field_semantics import (
    MilvusFieldSemanticIndexStore,
)
from invoice_intelligence.infrastructure.observability.ocr import SQLAlchemyOCRTelemetry
from invoice_intelligence.infrastructure.observability.retrieval import (
    ContextVarRetrievalTelemetry,
)
from invoice_intelligence.infrastructure.observability.trace import (
    ContextVarPrivacyTelemetry,
)
from invoice_intelligence.infrastructure.ocr.paddlex_http import PaddleXOCRHttpAdapter
from invoice_intelligence.infrastructure.persistence.database import (
    create_business_engine,
)
from invoice_intelligence.infrastructure.persistence.pgvector_memory import PgVectorMemoryStore
from invoice_intelligence.infrastructure.persistence.sqlalchemy_accounting import (
    SQLAlchemyAccountingRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_admission import (
    SQLAlchemyMemoryAdmissionRepository,
    SQLAlchemyMemoryConflictRepository,
    SQLAlchemyReviewerReliabilityRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation import (
    SQLAlchemyEvaluationRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_evaluation_jobs import (
    SQLAlchemyEvaluationJobRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_examples import (
    SQLAlchemyReviewedExampleRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_field_semantic_index import (
    SQLAlchemyFieldSemanticProjectionRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_field_semantics import (
    SQLAlchemyFieldAliasCandidateRepository,
    SQLAlchemyFieldAliasRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_governance import (
    SQLAlchemyMemoryGovernanceRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_model_training import (
    SQLAlchemyModelTrainingRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_promotion_candidates import (
    SQLAlchemyPromotionCandidateRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_promotion_evidence import (
    SQLAlchemyPromotionEvidenceRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_repository import (
    SQLAlchemyBusinessRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_review_tasks import (
    SQLAlchemyReviewTaskRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_stored_objects import (
    SQLAlchemyStoredObjectRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_training_registry import (
    SQLAlchemyTrainingRegistryRepository,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_transactions import (
    SQLAlchemyTransactionAnalysisRepository,
)
from invoice_intelligence.infrastructure.qwen.client import QwenRemoteClient
from invoice_intelligence.infrastructure.qwen.embeddings import QwenDenseEmbeddingProvider
from invoice_intelligence.infrastructure.qwen.memory_quality import (
    QwenMemoryQualityAssessmentProvider,
)
from invoice_intelligence.infrastructure.qwen.query_rewrite import (
    QwenQueryRewriteProvider,
)
from invoice_intelligence.infrastructure.qwen.redaction import QwenPayloadGuard
from invoice_intelligence.infrastructure.qwen.reranking import QwenRerankingProvider
from invoice_intelligence.infrastructure.qwen.vision import QwenVisionExtractionProvider
from invoice_intelligence.infrastructure.reporting.postgres_evaluation import (
    PostgreSQLEvaluationArtifactPublisher,
)
from invoice_intelligence.infrastructure.security.capabilities import (
    BUILTIN_PROVIDER_CAPABILITIES,
    require_builtin_capability,
)
from invoice_intelligence.infrastructure.serialization.pydantic import (
    PydanticExtractionStateCodec,
    PydanticInvoiceSchemaInspector,
)
from invoice_intelligence.infrastructure.storage.download_access import LocalDownloadAccessIssuer
from invoice_intelligence.infrastructure.storage.local import LocalFileStorage
from invoice_intelligence.infrastructure.storage.s3 import S3DownloadAccessIssuer, S3FileStorage
from invoice_intelligence.infrastructure.training.artifacts import SafeTrainingArtifactReader
from invoice_intelligence.infrastructure.training.mlflow_compatible import (
    MLflowCompatibleTrainingProvider,
)
from invoice_intelligence.infrastructure.training.stub import (
    DeterministicStubTrainingProvider,
)
from invoice_intelligence.infrastructure.vision.openai import OpenAIVisionExtractionProvider
from invoice_intelligence.infrastructure.vision.prompt_registry import LocalVisionPromptRegistry
from invoice_intelligence.workflow.graph import build_invoice_workflow
from invoice_intelligence.workflow.nodes import WorkflowDependencies
from invoice_intelligence.workflow.runner import InvoiceWorkflowRunner


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Concrete dependencies exposed to protocol adapters."""

    settings: Settings
    file_storage: FileStorage
    document_access_service: DocumentAccessService
    document_ingestion_service: DocumentIngestionService
    vision_extraction_service: VisionExtractionService[InvoiceExtraction] | None
    business_repository: SQLAlchemyBusinessRepository
    review_task_repository: SQLAlchemyReviewTaskRepository
    accounting_service: AccountingService
    reviewed_example_repository: SQLAlchemyReviewedExampleRepository
    field_alias_repository: SQLAlchemyFieldAliasRepository
    field_alias_candidate_repository: SQLAlchemyFieldAliasCandidateRepository
    field_alias_learning_service: FieldAliasLearningService[InvoiceExtraction]
    field_semantic_catalog: FieldSemanticCatalog[InvoiceExtraction]
    field_semantic_binding_service: FieldSemanticBindingService[InvoiceExtraction] | None
    field_semantic_projection_repository: SQLAlchemyFieldSemanticProjectionRepository
    field_semantic_index_projection_service: (
        FieldSemanticIndexProjectionService[InvoiceExtraction] | None
    )
    field_semantic_index_store: FieldSemanticIndexStore | None
    memory_admission_repository: SQLAlchemyMemoryAdmissionRepository
    memory_conflict_repository: SQLAlchemyMemoryConflictRepository
    reviewer_reliability_repository: SQLAlchemyReviewerReliabilityRepository
    memory_quality_validator: DeterministicMemoryQualityValidator[InvoiceExtraction]
    memory_quality_assessment_provider: MemoryQualityAssessmentProvider | None
    memory_admission_service: MemoryAdmissionService[InvoiceExtraction]
    extraction_validator: EvidenceBasedExtractionValidator
    extraction_state_codec: PydanticExtractionStateCodec
    correction_applier: PydanticHumanCorrectionApplier
    correction_memory_service: CorrectionMemoryService
    reviewed_example_generation_service: ReviewedExampleGenerationService[InvoiceExtraction]
    example_index_projection_service: ExampleIndexProjectionService | None
    example_retrieval_service: HybridExampleRetrievalService | None
    example_index_store: ExampleIndexStore | None
    dense_embedding_provider: DenseEmbeddingProvider | None
    reranking_provider: RerankingProvider | None
    query_rewrite_provider: QueryRewriteProvider | None
    raw_ocr_provider: RawOCRProvider | None
    raw_ocr_providers: tuple[RawOCRProvider, ...]
    ocr_comparison_service: DeterministicMultiSourceOCRComparisonService[InvoiceExtraction] | None
    memory_governance_service: MemoryGovernanceService
    promotion_candidate_service: PromotionCandidateService
    governance_repository: SQLAlchemyMemoryGovernanceRepository
    qwen_client: QwenRemoteClient | None
    privacy_telemetry: ContextVarPrivacyTelemetry
    authorization_policy: AuthorizationPolicy
    auth_context_provider: AuthContextProvider | None
    security_audit_sink: SecurityAuditSink
    transaction_analysis_service: TransactionAnalysisService
    training_registry_repository: SQLAlchemyTrainingRegistryRepository
    model_training_repository: SQLAlchemyModelTrainingRepository
    training_job_service: TrainingJobService
    evaluation_job_service: EvaluationJobService
    training_provider: TrainingProvider
    training_artifact_reader: TrainingArtifactReader


def build_container(
    settings: Settings | None = None,
    ocr_provider: OCRValidationProvider | None = None,
    *,
    raw_ocr_provider: RawOCRProvider | None = None,
    example_index_store: ExampleIndexStore | None = None,
    field_semantic_index_store: FieldSemanticIndexStore | None = None,
    dense_embedding_provider: DenseEmbeddingProvider | None = None,
    sparse_embedding_provider: SparseEmbeddingProvider | None = None,
    reranking_provider: RerankingProvider | None = None,
    field_semantic_reranking_provider: FieldSemanticRerankingProvider | None = None,
    query_rewrite_provider: QueryRewriteProvider | None = None,
    memory_quality_assessment_provider: MemoryQualityAssessmentProvider | None = None,
    dense_model_version: ModelVersion | None = None,
    sparse_model_version: ModelVersion | None = None,
    rerank_model_version: ModelVersion | None = None,
    vision_prompt_registry: VisionPromptRegistry | None = None,
    allow_legacy_storage_migration: bool = False,
) -> ApplicationContainer:
    """Build the application dependency container."""

    resolved_settings = settings or get_settings()
    resolved_vision_prompt_registry = vision_prompt_registry or LocalVisionPromptRegistry()
    business_engine = create_business_engine(
        resolved_settings.resolved_business_database_url.get_secret_value()
    )
    evaluation_repository = SQLAlchemyEvaluationRepository(business_engine)
    evaluation_job_service = EvaluationJobService(
        SQLAlchemyEvaluationJobRepository(business_engine),
        dataset_repository=evaluation_repository,
    )
    if (
        resolved_settings.environment.value == "production"
        and resolved_settings.file_storage_backend == "s3"
        and not allow_legacy_storage_migration
    ):
        with business_engine.connect() as connection:
            pending_legacy = connection.scalar(
                text(
                    "SELECT count(*) FROM stored_objects "
                    "WHERE status IN ('migration_pending', 'failed')"
                )
            )
        if pending_legacy:
            business_engine.dispose()
            raise ValueError(
                "Production object storage cannot start before legacy migration completes"
            )
    authorization_policy = AuthorizationPolicy()
    security_audit_sink = SQLAlchemySecurityAuditSink(business_engine)
    auth_context_provider: AuthContextProvider | None = None
    if resolved_settings.auth_mode == "oidc":
        assert resolved_settings.oidc_issuer is not None
        assert resolved_settings.oidc_audience is not None
        assert resolved_settings.oidc_jwks_url is not None
        auth_context_provider = OIDCJWTAuthContextProvider(
            issuer=resolved_settings.oidc_issuer,
            audience=resolved_settings.oidc_audience,
            jwks_url=resolved_settings.oidc_jwks_url,
            client_id=resolved_settings.oidc_client_id,
            algorithms=resolved_settings.oidc_algorithms,
            tenant_claim=resolved_settings.oidc_tenant_claim,
            reviewer_claim=resolved_settings.oidc_reviewer_claim,
            jwks_cache_seconds=resolved_settings.oidc_jwks_cache_seconds,
            timeout_seconds=resolved_settings.oidc_timeout_seconds,
            tls_verify=resolved_settings.oidc_tls_verify,
        )
    privacy_telemetry = ContextVarPrivacyTelemetry()
    ocr_telemetry = SQLAlchemyOCRTelemetry(business_engine)
    resolved_raw_ocr_providers: list[RawOCRProvider] = []
    if raw_ocr_provider is not None:
        resolved_raw_ocr_providers.append(raw_ocr_provider)
    elif resolved_settings.ocr_enabled:
        resolved_raw_ocr_providers.append(
            PaddleXOCRHttpAdapter(
                base_url=resolved_settings.ocr_base_url,
                endpoint_path=resolved_settings.ocr_endpoint_path,
                provider_version=resolved_settings.ocr_provider_version,
                model_version=resolved_settings.ocr_model_version,
                config_version=resolved_settings.ocr_pipeline_config_version,
                connect_timeout_seconds=resolved_settings.ocr_connect_timeout_seconds,
                read_timeout_seconds=resolved_settings.ocr_read_timeout_seconds,
                write_timeout_seconds=resolved_settings.ocr_write_timeout_seconds,
                pool_timeout_seconds=resolved_settings.ocr_pool_timeout_seconds,
                max_retries=resolved_settings.ocr_max_retries,
                max_concurrency=resolved_settings.ocr_max_concurrency,
                requests_per_minute=resolved_settings.ocr_requests_per_minute,
                backoff_base_seconds=resolved_settings.ocr_backoff_base_seconds,
                backoff_max_seconds=resolved_settings.ocr_backoff_max_seconds,
                circuit_failure_threshold=resolved_settings.ocr_circuit_failure_threshold,
                circuit_recovery_seconds=resolved_settings.ocr_circuit_recovery_seconds,
                max_input_bytes=resolved_settings.ocr_max_input_bytes,
                telemetry=ocr_telemetry,
            )
        )
    for remote in sorted(
        (item for item in resolved_settings.ocr_remote_providers if item.enabled),
        key=lambda item: item.provider_name,
    ):
        resolved_raw_ocr_providers.append(
            PaddleXOCRHttpAdapter(
                base_url=remote.base_url,
                endpoint_path=remote.endpoint_path,
                provider_name=remote.provider_name,
                provider_version=remote.provider_version,
                model_version=remote.model_version,
                config_version=remote.config_version,
                connect_timeout_seconds=remote.connect_timeout_seconds,
                read_timeout_seconds=remote.read_timeout_seconds,
                write_timeout_seconds=remote.write_timeout_seconds,
                pool_timeout_seconds=remote.pool_timeout_seconds,
                max_retries=remote.max_retries,
                max_concurrency=remote.max_concurrency,
                requests_per_minute=remote.requests_per_minute,
                backoff_base_seconds=remote.backoff_base_seconds,
                backoff_max_seconds=remote.backoff_max_seconds,
                circuit_failure_threshold=remote.circuit_failure_threshold,
                circuit_recovery_seconds=remote.circuit_recovery_seconds,
                max_input_bytes=remote.max_input_bytes,
                telemetry=ocr_telemetry,
            )
        )
    resolved_raw_ocr_providers_tuple = tuple(resolved_raw_ocr_providers)
    resolved_raw_ocr_provider = (
        resolved_raw_ocr_providers_tuple[0] if resolved_raw_ocr_providers_tuple else None
    )
    resolved_example_index_store = example_index_store
    if resolved_example_index_store is None and resolved_settings.milvus_enabled:
        milvus_uri = resolved_settings.milvus_uri
        if milvus_uri is None:
            raise ValueError("milvus_uri is required when the Milvus adapter is enabled")
        resolved_example_index_store = MilvusExampleIndexStore(
            uri=milvus_uri,
            token=(
                resolved_settings.milvus_token.get_secret_value()
                if resolved_settings.milvus_token is not None
                else None
            ),
            connect_timeout_seconds=resolved_settings.milvus_connect_timeout_seconds,
            operation_timeout_seconds=resolved_settings.milvus_operation_timeout_seconds,
            max_retries=resolved_settings.milvus_max_retries,
            consistency_level=resolved_settings.milvus_consistency_level,
            collection_resolver=DefaultMilvusCollectionResolver(
                collection_prefix=resolved_settings.milvus_collection_prefix,
                dedicated_collections=resolved_settings.milvus_dedicated_tenant_collections,
            ),
            alias=resolved_settings.milvus_alias,
            dense_dimension=resolved_settings.milvus_dense_dimension,
            hnsw_m=resolved_settings.milvus_hnsw_m,
            hnsw_ef_construction=resolved_settings.milvus_hnsw_ef_construction,
            search_ef=resolved_settings.milvus_search_ef,
            batch_size=resolved_settings.milvus_batch_size,
            sparse_enabled=resolved_settings.milvus_sparse_enabled,
            bm25_function_enabled=resolved_settings.milvus_bm25_function_enabled,
            sparse_metric_type=resolved_settings.milvus_sparse_metric_type,
            dense_weight=resolved_settings.milvus_dense_weight,
            sparse_weight=resolved_settings.milvus_sparse_weight,
            ranker=resolved_settings.milvus_ranker,
            max_text_length=resolved_settings.milvus_max_text_length,
        )
    resolved_field_semantic_index_store = field_semantic_index_store
    if (
        resolved_field_semantic_index_store is None
        and resolved_settings.field_semantic_index_enabled
    ):
        milvus_uri = resolved_settings.milvus_uri
        if milvus_uri is None:
            raise ValueError("milvus_uri is required for field semantic indexing")
        resolved_field_semantic_index_store = MilvusFieldSemanticIndexStore(
            uri=milvus_uri,
            token=(
                resolved_settings.milvus_token.get_secret_value()
                if resolved_settings.milvus_token is not None
                else None
            ),
            connect_timeout_seconds=resolved_settings.milvus_connect_timeout_seconds,
            operation_timeout_seconds=resolved_settings.milvus_operation_timeout_seconds,
            max_retries=resolved_settings.milvus_max_retries,
            consistency_level=resolved_settings.milvus_consistency_level,
            collection_resolver=DefaultMilvusCollectionResolver(
                collection_prefix=(resolved_settings.field_semantic_index_collection_prefix),
                dedicated_collections={
                    tenant_id: f"{prefix}_field_semantics"
                    for tenant_id, prefix in (
                        resolved_settings.milvus_dedicated_tenant_collections.items()
                    )
                },
            ),
            alias=resolved_settings.field_semantic_index_alias,
            dense_dimension=resolved_settings.milvus_dense_dimension,
            hnsw_m=resolved_settings.milvus_hnsw_m,
            hnsw_ef_construction=resolved_settings.milvus_hnsw_ef_construction,
            search_ef=resolved_settings.milvus_search_ef,
            batch_size=resolved_settings.milvus_batch_size,
            sparse_enabled=resolved_settings.milvus_sparse_enabled,
            bm25_function_enabled=resolved_settings.milvus_bm25_function_enabled,
            sparse_metric_type=resolved_settings.milvus_sparse_metric_type,
            max_text_length=resolved_settings.milvus_max_text_length,
        )
    limits = DocumentProcessingLimits(
        max_upload_bytes=resolved_settings.max_upload_bytes,
        max_pdf_pages=resolved_settings.max_pdf_pages,
        pdf_render_dpi=resolved_settings.pdf_render_dpi,
        max_image_dimension=resolved_settings.max_image_dimension,
        max_image_pixels=resolved_settings.max_image_pixels,
        max_total_rendered_pixels=resolved_settings.max_total_rendered_pixels,
        image_preprocessing_enabled=resolved_settings.image_preprocessing_enabled,
        image_preprocessing_contrast=resolved_settings.image_preprocessing_contrast,
        image_preprocessing_sharpness=resolved_settings.image_preprocessing_sharpness,
    )
    download_issuer: DownloadAccessIssuer
    download_verifier: LocalDownloadAccessIssuer | None
    if resolved_settings.file_storage_backend == "local":
        file_storage: FileStorage = LocalFileStorage(resolved_settings.file_storage_root)
        local_download_issuer = LocalDownloadAccessIssuer(
            resolved_settings.object_storage_tenant_hmac_key.get_secret_value().encode("utf-8"),
            resolved_settings.api_prefix,
        )
        download_issuer = local_download_issuer
        download_verifier = local_download_issuer
    else:
        def resolve_secret(value: SecretStr | None, path: Path | None) -> str | None:
            if value is not None:
                return value.get_secret_value()
            if path is None:
                return None
            content = path.read_text(encoding="utf-8").removesuffix("\n").removesuffix("\r")
            if not content:
                raise ValueError("Object storage secret file must not be empty")
            return content

        access_key = resolve_secret(
            resolved_settings.object_storage_access_key,
            resolved_settings.object_storage_access_key_file,
        )
        secret_key = resolve_secret(
            resolved_settings.object_storage_secret_key,
            resolved_settings.object_storage_secret_key_file,
        )
        client = boto3.client(
            "s3",
            endpoint_url=resolved_settings.object_storage_endpoint_url,
            region_name=resolved_settings.object_storage_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            verify=resolved_settings.object_storage_tls_verify,
            config=BotoConfig(
                connect_timeout=resolved_settings.object_storage_connect_timeout_seconds,
                read_timeout=resolved_settings.object_storage_read_timeout_seconds,
                retries={
                    "mode": "standard",
                    "max_attempts": resolved_settings.object_storage_max_attempts,
                },
                s3={"addressing_style": "path"},
            ),
        )
        s3_storage = S3FileStorage(
            client=client,
            buckets={
                ObjectKind.ORIGINAL: resolved_settings.object_storage_originals_bucket,
                ObjectKind.RENDERED: resolved_settings.object_storage_rendered_bucket,
                ObjectKind.DERIVED_TEXT: resolved_settings.object_storage_derived_text_bucket,
            },
            tenant_key_secret=(
                resolved_settings.object_storage_tenant_hmac_key.get_secret_value().encode("utf-8")
            ),
        )
        s3_storage.validate_startup()
        file_storage = s3_storage
        download_issuer = S3DownloadAccessIssuer(
            client,
            frozenset(
                {
                    resolved_settings.object_storage_originals_bucket,
                    resolved_settings.object_storage_rendered_bucket,
                    resolved_settings.object_storage_derived_text_bucket,
                }
            ),
        )
        download_verifier = None
    document_processor = PillowMuPdfDocumentProcessor()
    image_quality_analyzer = PillowImageQualityAnalyzer()
    extraction_codec = PydanticExtractionStateCodec()
    business_repository = SQLAlchemyBusinessRepository(
        business_engine,
        extraction_codec,
    )
    transaction_analysis_service = TransactionAnalysisService(
        SQLAlchemyTransactionAnalysisRepository(business_engine),
        MockTransactionRuleEngine(),
        business_repository,
        business_repository,
        resolved_settings.invoice_schema_version,
    )
    stored_object_repository = SQLAlchemyStoredObjectRepository(business_engine)
    artifact_service = DocumentArtifactService(
        file_storage,
        stored_object_repository,
        resolved_settings.object_storage_rendered_retention_days,
        resolved_settings.object_storage_derived_text_retention_days,
    )
    review_task_repository = SQLAlchemyReviewTaskRepository(business_engine)
    model_training_repository = SQLAlchemyModelTrainingRepository(business_engine)
    training_registry_repository = SQLAlchemyTrainingRegistryRepository(business_engine)
    if resolved_settings.training_provider == "mlflow_compatible":
        if (
            resolved_settings.training_remote_base_url is None
            or resolved_settings.training_remote_bearer_token is None
        ):
            raise ValueError(
                "MLflow-compatible training requires a base URL and bearer token"
            )
        training_provider: TrainingProvider = MLflowCompatibleTrainingProvider(
            base_url=resolved_settings.training_remote_base_url,
            submit_path=resolved_settings.training_remote_submit_path,
            job_path_template=resolved_settings.training_remote_job_path_template,
            cancel_path_template=resolved_settings.training_remote_cancel_path_template,
            bearer_token=(
                resolved_settings.training_remote_bearer_token.get_secret_value()
            ),
            timeout_seconds=resolved_settings.training_remote_timeout_seconds,
        )
    else:
        training_provider = DeterministicStubTrainingProvider()
    training_artifact_reader: TrainingArtifactReader = SafeTrainingArtifactReader(
        allowed_https_hosts=resolved_settings.training_artifact_allowed_https_hosts,
        max_bytes=resolved_settings.training_artifact_max_bytes,
        timeout_seconds=resolved_settings.training_remote_timeout_seconds,
        allowed_file_root=resolved_settings.training_artifact_file_root,
    )
    training_job_service = TrainingJobService(
        registry=training_registry_repository,
        run_repository=model_training_repository,
        provider_name=resolved_settings.training_provider,
    )
    accounting_repository = SQLAlchemyAccountingRepository(business_engine)
    accounting_service = AccountingService(
        repository=accounting_repository,
        idempotency_repository=business_repository,
        exchange_rate_provider=MockExchangeRateProvider(),
        posting_provider=MockAccountingPostingProvider(),
    )
    reviewed_example_repository = SQLAlchemyReviewedExampleRepository(
        business_engine,
        projection_max_attempts=resolved_settings.index_projection_worker_max_attempts,
        projection_backoff_base_seconds=resolved_settings.index_projection_worker_backoff_base_seconds,
        projection_backoff_max_seconds=resolved_settings.index_projection_worker_backoff_max_seconds,
    )
    field_alias_repository = SQLAlchemyFieldAliasRepository(business_engine)
    field_alias_candidate_repository = SQLAlchemyFieldAliasCandidateRepository(business_engine)
    field_semantic_projection_repository = SQLAlchemyFieldSemanticProjectionRepository(
        business_engine,
        projection_max_attempts=resolved_settings.index_projection_worker_max_attempts,
        projection_backoff_base_seconds=resolved_settings.index_projection_worker_backoff_base_seconds,
        projection_backoff_max_seconds=resolved_settings.index_projection_worker_backoff_max_seconds,
    )
    memory_admission_repository = SQLAlchemyMemoryAdmissionRepository(business_engine)
    memory_conflict_repository = SQLAlchemyMemoryConflictRepository(business_engine)
    reviewer_reliability_repository = SQLAlchemyReviewerReliabilityRepository(business_engine)
    governance_repository = SQLAlchemyMemoryGovernanceRepository(business_engine)
    promotion_candidate_repository = SQLAlchemyPromotionCandidateRepository(business_engine)
    promotion_candidate_service = PromotionCandidateService(
        repository=promotion_candidate_repository,
        evidence_repository=SQLAlchemyPromotionEvidenceRepository(
            business_engine,
            active_schema_version=resolved_settings.invoice_schema_version,
            active_prompt_version=resolved_settings.example_retrieval_prompt_version,
            active_threshold_version=resolved_settings.example_retrieval_threshold_version,
        ),
        policy=PromotionPolicy(automatic_promotion_enabled=False),
    )
    retrieval_telemetry = ContextVarRetrievalTelemetry()
    correction_redactor = PatternSensitiveDataRedactor(
        policy=resolved_settings.correction_memory_redaction_policy,
        field_patterns=resolved_settings.correction_memory_sensitive_field_patterns,
        hash_salt=(
            resolved_settings.correction_memory_hash_salt.get_secret_value()
            if resolved_settings.correction_memory_hash_salt is not None
            else None
        ),
        redact_reason=resolved_settings.correction_memory_redact_reason,
    )
    qwen_client: QwenRemoteClient | None = None
    qwen_payload_guard: QwenPayloadGuard | None = None
    qwen_selected = (
        resolved_settings.vision_provider == "qwen"
        or resolved_settings.embedding_provider == "qwen"
        or resolved_settings.reranking_provider == "qwen"
        or resolved_settings.query_rewrite_provider == "qwen"
        or resolved_settings.memory_quality_assessment_provider == "qwen"
    )
    if qwen_selected and resolved_settings.qwen_api_key is not None:
        compatible_base_url = resolved_settings.qwen_compatible_base_url
        rerank_base_url = resolved_settings.qwen_rerank_base_url
        qwen_client = QwenRemoteClient(
            api_key=resolved_settings.qwen_api_key.get_secret_value(),
            compatible_base_url=compatible_base_url,
            rerank_base_url=rerank_base_url,
            timeout_seconds=resolved_settings.qwen_timeout_seconds,
            max_retries=resolved_settings.qwen_max_retries,
            max_concurrency=resolved_settings.qwen_max_concurrency,
            requests_per_minute=resolved_settings.qwen_requests_per_minute,
            backoff_base_seconds=resolved_settings.qwen_backoff_base_seconds,
            backoff_max_seconds=resolved_settings.qwen_backoff_max_seconds,
            circuit_failure_threshold=resolved_settings.qwen_circuit_failure_threshold,
            circuit_recovery_seconds=resolved_settings.qwen_circuit_recovery_seconds,
            audit_enabled=resolved_settings.qwen_audit_enabled,
            telemetry=retrieval_telemetry,
            model_costs_per_million={
                model: (
                    rates.input_per_million_tokens,
                    rates.output_per_million_tokens,
                )
                for model, rates in resolved_settings.qwen_model_costs_per_million.items()
            },
        )
        qwen_payload_guard = QwenPayloadGuard(
            correction_redactor=correction_redactor,
            allow_vision_images=resolved_settings.qwen_allow_unredacted_vision_images,
        )
    resolved_memory_quality_assessment_provider = memory_quality_assessment_provider
    if (
        resolved_memory_quality_assessment_provider is None
        and resolved_settings.memory_quality_assessment_provider == "qwen"
        and qwen_client is not None
        and qwen_payload_guard is not None
    ):
        resolved_memory_quality_assessment_provider = QwenMemoryQualityAssessmentProvider(
            client=qwen_client,
            payload_guard=qwen_payload_guard,
            model=resolved_settings.qwen_memory_quality_assessment_model,
            prompt_version=(resolved_settings.qwen_memory_quality_assessment_prompt_version),
        )
    vector_store = None
    embedding_provider: DenseEmbeddingProvider | None = None
    if (
        resolved_settings.embedding_provider == "openai"
        and resolved_settings.openai_api_key is not None
    ):
        embedding_provider = OpenAIEmbeddingProvider(
            api_key=resolved_settings.openai_api_key.get_secret_value(),
            model=resolved_settings.correction_embedding_model,
            dimensions=resolved_settings.correction_memory_vector_dimensions,
            timeout_seconds=resolved_settings.correction_embedding_timeout_seconds,
            max_retries=resolved_settings.correction_embedding_max_retries,
            max_concurrency=resolved_settings.openai_max_concurrency,
            requests_per_minute=resolved_settings.openai_requests_per_minute,
            circuit_failure_threshold=(resolved_settings.openai_circuit_failure_threshold),
            circuit_recovery_seconds=(resolved_settings.openai_circuit_recovery_seconds),
        )
    elif (
        resolved_settings.embedding_provider == "qwen"
        and qwen_client is not None
        and qwen_payload_guard is not None
    ):
        embedding_provider = QwenDenseEmbeddingProvider(
            client=qwen_client,
            payload_guard=qwen_payload_guard,
            model=resolved_settings.qwen_embedding_model,
            dimensions=resolved_settings.qwen_embedding_dimensions,
            batch_size=resolved_settings.qwen_embedding_batch_size,
        )
    correction_embedding_provider = (
        embedding_provider
        if (
            resolved_settings.correction_memory_backend == "postgres_pgvector"
            and resolved_example_index_store is None
        )
        else None
    )
    if correction_embedding_provider is not None:
        vector_store = PgVectorMemoryStore(
            business_engine,
            resolved_settings.correction_memory_vector_dimensions,
        )
    correction_scope_resolver = PydanticCorrectionScopeResolver(
        vendor_feature_fields=resolved_settings.correction_vendor_feature_fields,
        template_feature_fields=resolved_settings.correction_template_feature_fields,
    )
    field_semantic_catalog = FieldSemanticCatalog(
        schema_reader=correction_scope_resolver,
        alias_repository=field_alias_repository,
        output_schema=InvoiceExtraction,
        schema_version=resolved_settings.invoice_schema_version,
        base_catalog_version=FieldSemanticCatalogVersion(
            resolved_settings.field_semantic_base_catalog_version
        ),
    )
    correction_memory_service = CorrectionMemoryService(
        event_repository=business_repository,
        vector_store=vector_store,
        embedding_provider=correction_embedding_provider,
        redactor=correction_redactor,
        scope_resolver=correction_scope_resolver,
        schema_version=resolved_settings.invoice_schema_version,
        min_similarity=resolved_settings.correction_memory_min_similarity,
        per_scope_limit=resolved_settings.correction_memory_per_scope_limit,
        max_scopes=resolved_settings.correction_memory_max_scopes,
    )
    reviewed_example_salt = resolved_settings.reviewed_example_fingerprint_salt.get_secret_value()
    feature_fingerprint_setting = resolved_settings.example_retrieval_feature_fingerprint_salt
    feature_fingerprint_salt = (
        feature_fingerprint_setting.get_secret_value()
        if feature_fingerprint_setting is not None
        else reviewed_example_salt
    )
    vision_model_version = ModelVersion(
        resolved_settings.qwen_vision_model
        if resolved_settings.vision_provider == "qwen"
        else resolved_settings.openai_model
    )
    reviewed_example_generation_service = ReviewedExampleGenerationService(
        correction_event_repository=business_repository,
        example_repository=reviewed_example_repository,
        admission_repository=memory_admission_repository,
        field_semantic_catalog=field_semantic_catalog,
        scope_resolver=correction_scope_resolver,
        output_schema=InvoiceExtraction,
        schema_version=resolved_settings.invoice_schema_version,
        model_version=vision_model_version,
        prompt_version=PromptVersion(resolved_settings.vision_prompt_version),
        admission_policy_version=resolved_settings.memory_admission_policy_version,
        fingerprint_salt=reviewed_example_salt,
        feature_fingerprint_salt=feature_fingerprint_salt,
    )
    example_index_projection_service = None
    field_semantic_index_projection_service = None
    index_integrity_verifier = SQLAlchemyMilvusIndexIntegrityVerifier(
        business_engine,
        example_store=(
            resolved_example_index_store
            if isinstance(resolved_example_index_store, MilvusExampleIndexStore)
            else None
        ),
        field_store=(
            resolved_field_semantic_index_store
            if isinstance(resolved_field_semantic_index_store, MilvusFieldSemanticIndexStore)
            else None
        ),
    )
    example_redactor: TenantExampleRedactor | None = None
    resolved_dense_provider = dense_embedding_provider or embedding_provider
    if resolved_field_semantic_index_store is not None:
        if resolved_dense_provider is None:
            raise ValueError("A field semantic index store requires a dense embedding provider")
        if not resolved_settings.milvus_bm25_function_enabled and sparse_embedding_provider is None:
            raise ValueError(
                "Field semantic indexing requires sparse embeddings when BM25 is disabled"
            )
        field_semantic_index_projection_service = FieldSemanticIndexProjectionService(
            catalog=field_semantic_catalog,
            projection_repository=field_semantic_projection_repository,
            index_store=resolved_field_semantic_index_store,
            dense_embedding_provider=resolved_dense_provider,
            sparse_embedding_provider=sparse_embedding_provider,
            batch_size=(resolved_settings.field_semantic_index_projection_batch_size),
            privacy_telemetry=privacy_telemetry,
            integrity_verifier=(
                index_integrity_verifier
                if isinstance(resolved_field_semantic_index_store, MilvusFieldSemanticIndexStore)
                else None
            ),
        )
    field_alias_learning_service = FieldAliasLearningService(
        catalog=field_semantic_catalog,
        alias_repository=field_alias_repository,
        candidate_repository=field_alias_candidate_repository,
        conflict_repository=memory_conflict_repository,
        index_projection_service=field_semantic_index_projection_service,
        policy=FieldAliasLearningPolicy(
            version=resolved_settings.field_alias_learning_policy_version,
            support_window_days=resolved_settings.field_alias_support_window_days,
            min_distinct_documents=(resolved_settings.field_alias_min_distinct_documents),
            min_distinct_templates=(resolved_settings.field_alias_min_distinct_templates),
            min_distinct_reviewers=(resolved_settings.field_alias_min_distinct_reviewers),
            global_min_distinct_tenants=(resolved_settings.field_alias_global_min_distinct_tenants),
        ),
        schema_version=resolved_settings.invoice_schema_version,
        fingerprint_salt=reviewed_example_salt,
        template_fingerprint_salt=feature_fingerprint_salt,
        global_hash_salt=(
            resolved_settings.field_alias_global_hash_salt.get_secret_value()
            if resolved_settings.field_alias_global_hash_salt is not None
            else None
        ),
        global_hash_salt_version=(resolved_settings.field_alias_global_hash_salt_version),
    )
    if resolved_example_index_store is not None:
        if resolved_dense_provider is None:
            raise ValueError(
                "An example index store requires a configured dense embedding provider"
            )
        default_hash_salt = (
            resolved_settings.example_index_hash_salt.get_secret_value()
            if resolved_settings.example_index_hash_salt is not None
            else None
        )
        tenant_policies = {
            tenant_id: TenantRedactionPolicy(
                strategy=policy.strategy,
                policy_version=resolved_settings.example_index_redaction_policy_version,
                hash_salt=(
                    policy.hash_salt.get_secret_value()
                    if policy.hash_salt is not None
                    else default_hash_salt
                ),
            )
            for tenant_id, policy in resolved_settings.example_index_tenant_policies.items()
        }
        example_redactor = TenantExampleRedactor(
            default_policy=TenantRedactionPolicy(
                strategy=resolved_settings.example_index_redaction_policy,
                policy_version=resolved_settings.example_index_redaction_policy_version,
                hash_salt=default_hash_salt,
            ),
            tenant_policies=tenant_policies,
            field_patterns=resolved_settings.correction_memory_sensitive_field_patterns,
            redact_reason=resolved_settings.correction_memory_redact_reason,
        )
        example_index_projection_service = ExampleIndexProjectionService(
            projection_repository=reviewed_example_repository,
            admission_repository=memory_admission_repository,
            redactor=example_redactor,
            index_store=resolved_example_index_store,
            dense_embedding_provider=resolved_dense_provider,
            sparse_embedding_provider=sparse_embedding_provider,
            redaction_policy_version=resolved_settings.example_index_redaction_policy_version,
            batch_size=resolved_settings.example_index_projection_batch_size,
            retention_days=resolved_settings.reviewed_example_retention_days,
            privacy_telemetry=privacy_telemetry,
            integrity_verifier=(
                index_integrity_verifier
                if isinstance(resolved_example_index_store, MilvusExampleIndexStore)
                else None
            ),
        )
    ingestion_service = DocumentIngestionService(
        file_storage=file_storage,
        document_processor=document_processor,
        document_repository=business_repository,
        idempotency_repository=business_repository,
        limits=limits,
        privacy_telemetry=privacy_telemetry,
        original_retention_days=resolved_settings.object_storage_original_retention_days,
    )
    document_access_service = DocumentAccessService(
        repository=business_repository,
        storage=file_storage,
        issuer=download_issuer,
        ttl_seconds=resolved_settings.object_storage_presign_ttl_seconds,
        verifier=download_verifier,
    )

    resolved_reranking_provider = reranking_provider
    if (
        resolved_reranking_provider is None
        and resolved_settings.reranking_provider == "qwen"
        and qwen_client is not None
        and qwen_payload_guard is not None
    ):
        resolved_reranking_provider = QwenRerankingProvider(
            client=qwen_client,
            payload_guard=qwen_payload_guard,
            model=resolved_settings.qwen_rerank_model,
            instruct=resolved_settings.qwen_rerank_instruct,
            max_documents=resolved_settings.qwen_rerank_max_documents,
        )
    resolved_query_rewrite_provider = query_rewrite_provider
    if (
        resolved_query_rewrite_provider is None
        and resolved_settings.query_rewrite_provider == "qwen"
        and qwen_client is not None
        and qwen_payload_guard is not None
    ):
        resolved_query_rewrite_provider = QwenQueryRewriteProvider(
            client=qwen_client,
            payload_guard=qwen_payload_guard,
            model=resolved_settings.qwen_query_rewrite_model,
            prompt_version=resolved_settings.qwen_query_rewrite_prompt_version,
            max_expansion_terms=resolved_settings.qwen_query_rewrite_max_expansion_terms,
        )

    resolved_field_semantic_reranker = field_semantic_reranking_provider
    if (
        resolved_field_semantic_reranker is None
        and resolved_reranking_provider is not None
        and callable(getattr(resolved_reranking_provider, "rerank_field_semantics", None))
    ):
        resolved_field_semantic_reranker = cast(
            FieldSemanticRerankingProvider,
            resolved_reranking_provider,
        )

    resolved_dense_model_version = dense_model_version
    if resolved_dense_model_version is None and resolved_dense_provider is not None:
        if resolved_settings.embedding_provider == "qwen":
            resolved_dense_model_version = ModelVersion(resolved_settings.qwen_embedding_model)
        elif resolved_settings.embedding_provider == "openai":
            resolved_dense_model_version = ModelVersion(
                resolved_settings.correction_embedding_model
            )
    resolved_sparse_model_version = sparse_model_version
    if (
        resolved_sparse_model_version is None
        and sparse_embedding_provider is None
        and resolved_settings.milvus_sparse_enabled
        and resolved_settings.milvus_bm25_function_enabled
    ):
        resolved_sparse_model_version = ModelVersion("milvus-bm25")
    resolved_rerank_model_version = rerank_model_version
    if (
        resolved_rerank_model_version is None
        and resolved_reranking_provider is not None
        and resolved_settings.reranking_provider == "qwen"
    ):
        resolved_rerank_model_version = ModelVersion(resolved_settings.qwen_rerank_model)

    if resolved_example_index_store is not None and resolved_reranking_provider is not None:
        if resolved_dense_model_version is None:
            raise ValueError("Example retrieval requires an explicit dense model version")
        if resolved_rerank_model_version is None:
            raise ValueError("Example retrieval requires an explicit rerank model version")
        if sparse_embedding_provider is not None and resolved_sparse_model_version is None:
            raise ValueError("Remote sparse retrieval requires an explicit model version")

    example_retrieval_service = None
    if (
        resolved_example_index_store is not None
        and example_redactor is not None
        and resolved_dense_provider is not None
        and resolved_reranking_provider is not None
        and resolved_dense_model_version is not None
        and resolved_rerank_model_version is not None
    ):
        fingerprint_salt = resolved_settings.example_retrieval_feature_fingerprint_salt
        if fingerprint_salt is None:
            raise ValueError("Example retrieval requires a feature fingerprint salt")
        example_retrieval_service = HybridExampleRetrievalService(
            projection_repository=reviewed_example_repository,
            admission_repository=memory_admission_repository,
            index_store=resolved_example_index_store,
            redactor=example_redactor,
            dense_embedding_provider=resolved_dense_provider,
            sparse_embedding_provider=sparse_embedding_provider,
            reranking_provider=resolved_reranking_provider,
            query_rewrite_provider=resolved_query_rewrite_provider,
            policy=_build_example_retrieval_policy(resolved_settings),
            dense_model_version=resolved_dense_model_version,
            sparse_model_version=resolved_sparse_model_version,
            rerank_model_version=resolved_rerank_model_version,
            prompt_version=PromptVersion(resolved_settings.example_retrieval_prompt_version),
            query_facts_resolver=correction_scope_resolver,
            field_semantic_catalog=field_semantic_catalog,
            schema_version=resolved_settings.invoice_schema_version,
            feature_fingerprint_salt=fingerprint_salt.get_secret_value(),
            max_fields=resolved_settings.example_retrieval_max_fields,
            retention_days=resolved_settings.reviewed_example_retention_days,
            telemetry_repository=governance_repository,
            telemetry_context=retrieval_telemetry,
            threshold_version=resolved_settings.example_retrieval_threshold_version,
        )

    field_semantic_binding_service = None
    if resolved_settings.field_semantic_binding_enabled:
        if resolved_field_semantic_index_store is None:
            raise ValueError("Field semantic binding requires its dedicated index store")
        if resolved_dense_provider is None:
            raise ValueError("Field semantic binding requires dense embeddings")
        if resolved_field_semantic_reranker is None:
            raise ValueError("Field semantic binding requires a compatible reranker")
        field_semantic_binding_service = FieldSemanticBindingService(
            catalog=field_semantic_catalog,
            projection_repository=field_semantic_projection_repository,
            index_store=resolved_field_semantic_index_store,
            dense_embedding_provider=resolved_dense_provider,
            sparse_embedding_provider=sparse_embedding_provider,
            reranking_provider=resolved_field_semantic_reranker,
            alias_learning_service=field_alias_learning_service,
            query_facts_resolver=correction_scope_resolver,
            policy=_build_field_semantic_binding_policy(resolved_settings),
            schema_version=resolved_settings.invoice_schema_version,
        )

    schema_inspector = PydanticInvoiceSchemaInspector()
    ocr_comparison_service = None
    if resolved_raw_ocr_providers_tuple and field_semantic_binding_service is not None:
        ocr_comparison_service = DeterministicMultiSourceOCRComparisonService(
            catalog=field_semantic_catalog,
            binding_service=field_semantic_binding_service,
            schema_inspector=schema_inspector,
            schema_version=resolved_settings.invoice_schema_version,
            policy=OCRComparisonPolicy(
                version=resolved_settings.ocr_comparison_policy_version,
                provider_score_thresholds=(
                    resolved_settings.ocr_comparison_provider_score_thresholds
                ),
                default_provider_score_threshold=(
                    resolved_settings.ocr_comparison_default_provider_score_threshold
                ),
                covered_field_paths=frozenset(resolved_settings.ocr_comparison_covered_field_paths),
                field_risk_levels={
                    field_path: OCRComparisonRiskLevel(risk)
                    for field_path, risk in (
                        resolved_settings.ocr_comparison_field_risk_levels.items()
                    )
                },
                default_field_risk_level=OCRComparisonRiskLevel(
                    resolved_settings.ocr_comparison_default_field_risk_level
                ),
                review_risk_levels=frozenset(
                    OCRComparisonRiskLevel(risk)
                    for risk in resolved_settings.ocr_comparison_review_risk_levels
                ),
                amount_tolerance=resolved_settings.ocr_comparison_amount_tolerance,
                max_context_observations=(
                    resolved_settings.ocr_comparison_max_context_observations
                ),
            ),
            telemetry=ocr_telemetry,
        )

    extraction_service: VisionExtractionService[InvoiceExtraction] | None = None
    provider: VisionExtractionProvider | None
    if (
        resolved_settings.vision_provider == "openai"
        and resolved_settings.openai_api_key is not None
    ):
        provider = OpenAIVisionExtractionProvider(
            api_key=resolved_settings.openai_api_key.get_secret_value(),
            model=resolved_settings.openai_model,
            timeout_seconds=resolved_settings.openai_timeout_seconds,
            max_retries=resolved_settings.openai_max_retries,
            schema_max_retries=(resolved_settings.openai_vision_schema_max_retries),
            image_detail=resolved_settings.openai_image_detail,
            max_concurrency=resolved_settings.openai_max_concurrency,
            requests_per_minute=resolved_settings.openai_requests_per_minute,
            circuit_failure_threshold=(resolved_settings.openai_circuit_failure_threshold),
            circuit_recovery_seconds=(resolved_settings.openai_circuit_recovery_seconds),
            prompt_version=resolved_settings.vision_prompt_version,
            prompt_registry=resolved_vision_prompt_registry,
        )
    elif (
        resolved_settings.vision_provider == "qwen"
        and qwen_client is not None
        and qwen_payload_guard is not None
    ):
        provider = QwenVisionExtractionProvider(
            client=qwen_client,
            payload_guard=qwen_payload_guard,
            model=resolved_settings.qwen_vision_model,
            schema_max_retries=resolved_settings.qwen_vision_schema_max_retries,
            prompt_version=resolved_settings.vision_prompt_version,
            prompt_registry=resolved_vision_prompt_registry,
        )
    else:
        provider = None
    if provider is not None:
        extraction_service = VisionExtractionService(
            file_storage=file_storage,
            artifact_service=artifact_service,
            document_processor=document_processor,
            provider=provider,
            image_quality_analyzer=image_quality_analyzer,
            limits=limits,
            field_semantic_catalog=field_semantic_catalog,
            correction_scope_resolver=correction_scope_resolver,
            schema_version=resolved_settings.invoice_schema_version,
            field_semantic_binding_service=field_semantic_binding_service,
            ocr_provider=ocr_provider,
            raw_ocr_providers=resolved_raw_ocr_providers_tuple,
            ocr_comparison_service=ocr_comparison_service,
            prompt_context_budget=PromptContextBudget(
                max_examples_total=resolved_settings.vision_prompt_max_examples_total,
                max_examples_per_region=(resolved_settings.vision_prompt_max_examples_per_region),
                max_correction_events=(resolved_settings.vision_prompt_max_correction_events),
                max_catalog_definitions=(resolved_settings.vision_prompt_max_catalog_definitions),
                max_section_chars=resolved_settings.vision_prompt_max_section_chars,
                max_total_chars=resolved_settings.vision_prompt_max_total_chars,
            ),
            privacy_telemetry=privacy_telemetry,
        )

    validation_policy = ValidationPolicy(
        defaults=ValidationThresholds(
            accept_threshold=resolved_settings.validation_accept_threshold,
            reject_threshold=resolved_settings.validation_reject_threshold,
            min_clarity=resolved_settings.validation_min_clarity,
            min_width=resolved_settings.validation_min_width,
            min_height=resolved_settings.validation_min_height,
            ocr_match_threshold=resolved_settings.validation_ocr_match_threshold,
            amount_tolerance=resolved_settings.validation_amount_tolerance,
        ),
        field_overrides={
            field_path: ValidationThresholdOverride(**override.model_dump())
            for field_path, override in resolved_settings.validation_field_overrides.items()
        },
    )
    memory_quality_field_risk_levels = {
        field_path: FieldRiskLevel(risk)
        for field_path, risk in (resolved_settings.memory_quality_field_risk_levels.items())
    }
    memory_quality_validator = DeterministicMemoryQualityValidator(
        schema_inspector=correction_scope_resolver,
        output_schema=InvoiceExtraction,
        policy=MemoryQualityValidationPolicy(
            version=resolved_settings.memory_admission_policy_version,
            active_schema_version=resolved_settings.invoice_schema_version,
            extraction_validation=validation_policy,
            min_distinct_documents=(resolved_settings.memory_quality_min_distinct_documents),
            min_distinct_templates=(resolved_settings.memory_quality_min_distinct_templates),
            min_distinct_reviewers=(resolved_settings.memory_quality_min_distinct_reviewers),
            max_text_length=resolved_settings.memory_quality_max_text_length,
            max_control_character_ratio=(
                resolved_settings.memory_quality_max_control_character_ratio
            ),
            max_repeated_character_run=(
                resolved_settings.memory_quality_max_repeated_character_run
            ),
            field_risk_levels=memory_quality_field_risk_levels,
        ),
    )
    extraction_validator = EvidenceBasedExtractionValidator(
        validation_policy,
        schema_inspector,
    )
    memory_admission_policy = DeterministicMemoryAdmissionPolicy(
        MemoryAdmissionPolicyConfig(
            version=resolved_settings.memory_admission_policy_version,
            field_risk_levels=memory_quality_field_risk_levels,
            auto_approval_risk_levels=frozenset(
                FieldRiskLevel(risk)
                for risk in (resolved_settings.memory_admission_auto_approval_risk_levels)
            ),
            minimum_quality_scores={
                FieldRiskLevel(risk): score
                for risk, score in (resolved_settings.memory_admission_min_quality_scores.items())
            },
            minimum_reviewer_reliability_score=(
                resolved_settings.memory_admission_min_reviewer_reliability_score
            ),
        )
    )
    memory_admission_service = MemoryAdmissionService(
        recovery_repository=business_repository,
        extraction_codec=extraction_codec,
        generation_service=reviewed_example_generation_service,
        example_repository=reviewed_example_repository,
        admission_repository=memory_admission_repository,
        conflict_repository=memory_conflict_repository,
        reviewer_reliability_repository=reviewer_reliability_repository,
        quality_validator=memory_quality_validator,
        quality_assessment_provider=resolved_memory_quality_assessment_provider,
        admission_policy=memory_admission_policy,
        schema_inspector=correction_scope_resolver,
        extraction_validator=extraction_validator,
        document_repository=business_repository,
        business_query_repository=business_repository,
        file_storage=file_storage,
        document_processor=document_processor,
        document_limits=limits,
        output_schema=InvoiceExtraction,
        policy_version=resolved_settings.memory_admission_policy_version,
        reviewer_profile_version=(resolved_settings.memory_admission_reviewer_profile_version),
        mandatory_business_rules=(resolved_settings.memory_admission_mandatory_business_rules),
        field_business_rules=resolved_settings.memory_admission_field_business_rules,
        projection_service=example_index_projection_service,
    )

    memory_governance_service = MemoryGovernanceService(
        example_repository=reviewed_example_repository,
        governance_repository=governance_repository,
        telemetry_repository=governance_repository,
        evaluation_repository=evaluation_repository,
        idempotency_repository=business_repository,
        projection_service=example_index_projection_service,
        admission_repository=memory_admission_repository,
        conflict_repository=memory_conflict_repository,
        field_alias_candidate_repository=field_alias_candidate_repository,
        field_alias_learning_service=field_alias_learning_service,
        field_semantic_catalog=field_semantic_catalog,
        admission_policy_version=resolved_settings.memory_admission_policy_version,
        conflict_resolution_policy_version=(
            resolved_settings.memory_conflict_resolution_policy_version
        ),
        schema_version=resolved_settings.invoice_schema_version,
        require_distinct_second_reviewer=(
            resolved_settings.memory_admission_require_distinct_second_reviewer
        ),
        ocr_metrics_repository=ocr_telemetry,
        field_semantic_projection_service=field_semantic_index_projection_service,
    )

    capability_bindings: tuple[tuple[object | None, ProviderCapability], ...] = (
        (provider, ProviderCapability.VISION_EXTRACTION),
        (resolved_dense_provider, ProviderCapability.DENSE_EMBEDDING),
        (resolved_reranking_provider, ProviderCapability.RERANKING),
        (resolved_query_rewrite_provider, ProviderCapability.QUERY_REWRITE),
        (
            resolved_memory_quality_assessment_provider,
            ProviderCapability.MEMORY_QUALITY_ADVISORY,
        ),
        (resolved_example_index_store, ProviderCapability.DERIVED_INDEX_WRITE),
        (resolved_field_semantic_index_store, ProviderCapability.DERIVED_INDEX_WRITE),
    )
    for adapter, capability in capability_bindings:
        if adapter is not None and type(adapter) in BUILTIN_PROVIDER_CAPABILITIES:
            require_builtin_capability(adapter, capability)
    for adapter in resolved_raw_ocr_providers_tuple:
        if type(adapter) in BUILTIN_PROVIDER_CAPABILITIES:
            require_builtin_capability(adapter, ProviderCapability.OCR_OBSERVATION)

    return ApplicationContainer(
        settings=resolved_settings,
        file_storage=file_storage,
        document_access_service=document_access_service,
        document_ingestion_service=ingestion_service,
        vision_extraction_service=extraction_service,
        business_repository=business_repository,
        review_task_repository=review_task_repository,
        accounting_service=accounting_service,
        reviewed_example_repository=reviewed_example_repository,
        field_alias_repository=field_alias_repository,
        field_alias_candidate_repository=field_alias_candidate_repository,
        field_alias_learning_service=field_alias_learning_service,
        field_semantic_catalog=field_semantic_catalog,
        field_semantic_binding_service=field_semantic_binding_service,
        field_semantic_projection_repository=field_semantic_projection_repository,
        field_semantic_index_projection_service=(field_semantic_index_projection_service),
        field_semantic_index_store=resolved_field_semantic_index_store,
        memory_admission_repository=memory_admission_repository,
        memory_conflict_repository=memory_conflict_repository,
        reviewer_reliability_repository=reviewer_reliability_repository,
        memory_quality_validator=memory_quality_validator,
        memory_quality_assessment_provider=(resolved_memory_quality_assessment_provider),
        memory_admission_service=memory_admission_service,
        extraction_validator=extraction_validator,
        extraction_state_codec=extraction_codec,
        correction_applier=PydanticHumanCorrectionApplier(
            schema_version=resolved_settings.invoice_schema_version,
            vendor_feature_fields=resolved_settings.correction_vendor_feature_fields,
            template_feature_fields=resolved_settings.correction_template_feature_fields,
        ),
        correction_memory_service=correction_memory_service,
        reviewed_example_generation_service=reviewed_example_generation_service,
        example_index_projection_service=example_index_projection_service,
        example_retrieval_service=example_retrieval_service,
        example_index_store=resolved_example_index_store,
        dense_embedding_provider=resolved_dense_provider,
        reranking_provider=resolved_reranking_provider,
        query_rewrite_provider=resolved_query_rewrite_provider,
        raw_ocr_provider=resolved_raw_ocr_provider,
        raw_ocr_providers=resolved_raw_ocr_providers_tuple,
        ocr_comparison_service=ocr_comparison_service,
        memory_governance_service=memory_governance_service,
        promotion_candidate_service=promotion_candidate_service,
        governance_repository=governance_repository,
        qwen_client=qwen_client,
        privacy_telemetry=privacy_telemetry,
        authorization_policy=authorization_policy,
        auth_context_provider=auth_context_provider,
        security_audit_sink=security_audit_sink,
        transaction_analysis_service=transaction_analysis_service,
        training_registry_repository=training_registry_repository,
        model_training_repository=model_training_repository,
        training_job_service=training_job_service,
        evaluation_job_service=evaluation_job_service,
        training_provider=training_provider,
        training_artifact_reader=training_artifact_reader,
    )


def build_evaluation_worker_service(
    engine: Engine, settings: Settings
) -> tuple[EvaluationJobService, SQLAlchemyEvaluationJobRepository]:
    """仅在隔离 Runner 凭据完整时启用 Suite 执行；报告写入 PostgreSQL。"""

    jobs = SQLAlchemyEvaluationJobRepository(engine)
    if settings.evaluation_runner_endpoint is None:
        if settings.evaluation_runner_token_file:
            raise ValueError("Evaluation Runner configuration is incomplete")
        return EvaluationJobService(jobs), jobs
    if settings.evaluation_runner_token_file is None:
        raise ValueError("Evaluation Runner requires a token file")
    token = settings.evaluation_runner_token_file.read_text(encoding="utf-8").strip()
    config = IsolatedEvaluationRunnerConfig(
        endpoint=settings.evaluation_runner_endpoint,
        bearer_token=token,
        timeout_seconds=settings.evaluation_runner_timeout_seconds,
        max_retries=settings.evaluation_runner_max_retries,
    )
    dataset_repository = SQLAlchemyEvaluationRepository(engine)
    suite_evaluation = OfflineEvaluationService(
        dataset_repository=dataset_repository,
        run_repository=dataset_repository,
        variant_executor=ConfiguredEvaluationVariantExecutor(
            {
                variant: IsolatedHTTPEvaluationVariantRunner(variant, config)
                for variant in ALL_EVALUATION_VARIANTS
            },
            maximum_concurrency=settings.evaluation_runner_max_concurrency,
        ),
        artifact_publisher=PostgreSQLEvaluationArtifactPublisher(engine),
        policy=OfflineEvaluationPolicy(),
    )
    service = EvaluationJobService(
        jobs,
        dataset_repository=dataset_repository,
        suite_evaluation=suite_evaluation,
        suite_run_timeout_seconds=settings.evaluation_suite_timeout_seconds,
    )
    return service, jobs


async def close_application_container(container: ApplicationContainer) -> None:
    """Close resources shared by protocol and standalone process entrypoints."""

    resources = (
        *container.raw_ocr_providers,
        container.example_index_store,
        container.field_semantic_index_store,
        container.qwen_client,
        container.auth_context_provider,
        container.training_provider,
        container.training_artifact_reader,
    )
    closed: set[int] = set()
    try:
        for resource in resources:
            if resource is None or id(resource) in closed:
                continue
            closed.add(id(resource))
            close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
            if callable(close):
                result = close()
                if isawaitable(result):
                    await result
    finally:
        container.business_repository.close()


def _build_field_semantic_binding_policy(
    settings: Settings,
) -> FieldSemanticBindingPolicy:
    return FieldSemanticBindingPolicy(
        candidate_k=settings.field_semantic_binding_candidate_k,
        result_limit=settings.field_semantic_binding_result_limit,
        fusion_strategy=settings.field_semantic_binding_fusion_strategy,
        dense_weight=settings.field_semantic_binding_dense_weight,
        sparse_weight=settings.field_semantic_binding_sparse_weight,
        min_candidate_score=settings.field_semantic_binding_min_candidate_score,
        acceptance_score=settings.field_semantic_binding_acceptance_score,
        min_score_margin=settings.field_semantic_binding_min_score_margin,
        min_rerank_score=settings.field_semantic_binding_min_rerank_score,
        exact_weight=settings.field_semantic_binding_exact_weight,
        retrieval_weight=settings.field_semantic_binding_retrieval_weight,
        rerank_weight=settings.field_semantic_binding_rerank_weight,
        context_weight=settings.field_semantic_binding_context_weight,
        position_weight=settings.field_semantic_binding_position_weight,
        value_type_weight=settings.field_semantic_binding_value_type_weight,
        require_value_type_for_acceptance=(
            settings.field_semantic_binding_require_value_type_for_acceptance
        ),
        version=settings.field_semantic_binding_policy_version,
    )


def _build_example_retrieval_policy(settings: Settings) -> HybridRetrievalPolicy:
    def category(config: object) -> CategoryRetrievalPolicy:
        candidate_k = int(getattr(config, "candidate_k"))
        top_k = int(getattr(config, "top_k"))
        min_relevance_score = float(getattr(config, "min_relevance_score"))
        configured_dense_weight = getattr(config, "dense_weight")
        configured_sparse_weight = getattr(config, "sparse_weight")
        return CategoryRetrievalPolicy(
            candidate_k=candidate_k,
            top_k=top_k,
            min_relevance_score=min_relevance_score,
            dense_weight=(
                float(configured_dense_weight)
                if configured_dense_weight is not None
                else settings.milvus_dense_weight
            ),
            sparse_weight=(
                float(configured_sparse_weight)
                if configured_sparse_weight is not None
                else settings.milvus_sparse_weight
            ),
        )

    defaults = {
        ExampleLabelType.CONFIRMED_CORRECT: category(settings.example_retrieval_confirmed_correct),
        ExampleLabelType.CORRECTED: category(settings.example_retrieval_corrected),
        ExampleLabelType.CONFIRMED_INCORRECT: category(
            settings.example_retrieval_confirmed_incorrect
        ),
    }
    field_overrides: dict[
        str,
        dict[ExampleLabelType, CategoryRetrievalPolicy],
    ] = {}
    for field_path, configured_labels in settings.example_retrieval_field_overrides.items():
        resolved_labels: dict[ExampleLabelType, CategoryRetrievalPolicy] = {}
        for label_name, configured_override in configured_labels.items():
            label = ExampleLabelType(label_name)
            resolved_labels[label] = replace(
                defaults[label],
                **configured_override.model_dump(exclude_none=True),
            )
        field_overrides[field_path] = resolved_labels
    return HybridRetrievalPolicy(
        fusion_strategy=settings.milvus_ranker,
        defaults=defaults,
        field_overrides=field_overrides,
        version=RetrievalPolicyVersion(settings.example_retrieval_policy_version),
    )


def build_workflow_dependencies(
    container: ApplicationContainer,
) -> WorkflowDependencies[InvoiceExtraction]:
    """Bind the fixed InvoiceExtraction schema to workflow application boundaries."""

    extraction_service = container.vision_extraction_service
    if extraction_service is None:
        raise RuntimeError("Vision extraction is not configured")
    repository = container.business_repository
    return WorkflowDependencies(
        output_schema=InvoiceExtraction,
        extraction_service=extraction_service,
        extraction_validator=container.extraction_validator,
        extraction_codec=container.extraction_state_codec,
        correction_applier=container.correction_applier,
        review_repository=repository,
        run_repository=repository,
        result_repository=repository,
        correction_memory_repository=container.correction_memory_service,
        memory_admission_service=container.memory_admission_service,
        field_semantic_binding_service=container.field_semantic_binding_service,
        reviewed_example_context_provider=container.example_retrieval_service,
        retrieval_telemetry_repository=container.governance_repository,
        correction_context_limit=container.settings.correction_context_limit,
        memory_recovery_lease_seconds=(container.settings.memory_admission_worker_lease_seconds),
        privacy_telemetry=container.privacy_telemetry,
    )


@asynccontextmanager
async def open_invoice_workflow_runner(
    container: ApplicationContainer,
) -> AsyncIterator[InvoiceWorkflowRunner | None]:
    """Keep the concrete InvoiceExtraction graph and checkpointer alive for the app lifespan."""

    if container.vision_extraction_service is None:
        yield None
        return

    dependencies = build_workflow_dependencies(container)
    async with open_checkpointer(container.settings) as checkpointer:
        graph = build_invoice_workflow(dependencies, checkpointer)
        yield InvoiceWorkflowRunner(graph)


@asynccontextmanager
async def open_extraction_workflow_service(
    container: ApplicationContainer,
) -> AsyncIterator[ExtractionWorkflowService]:
    """Compose the application workflow service for one API lifespan."""

    async with open_invoice_workflow_runner(container) as runner:
        repository = container.business_repository
        yield ExtractionWorkflowService(
            workflow=runner,
            document_repository=repository,
            query_repository=repository,
            idempotency_repository=repository,
            privacy_telemetry=container.privacy_telemetry,
        )
