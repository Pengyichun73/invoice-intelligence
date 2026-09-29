"""FastAPI dependency adapters exposing application services only."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import Depends, Request

from invoice_intelligence.application.errors import ForbiddenError, ServiceUnavailableError
from invoice_intelligence.application.ports.auth import AuthContextProvider, SecurityAuditSink
from invoice_intelligence.application.ports.observability import PrivacyTelemetry
from invoice_intelligence.application.services.accounting import AccountingService
from invoice_intelligence.application.services.authorization import AuthorizationPolicy
from invoice_intelligence.application.services.document_access import DocumentAccessService
from invoice_intelligence.application.services.document_ingestion import (
    DocumentIngestionService,
)
from invoice_intelligence.application.services.invoice_batches import InvoiceBatchService
from invoice_intelligence.application.services.evaluation_jobs import EvaluationJobService
from invoice_intelligence.application.services.extraction_workflow import (
    ExtractionWorkflowService,
)
from invoice_intelligence.application.services.memory_effectiveness import (
    MemoryEffectivenessService,
)
from invoice_intelligence.application.services.memory_gold import MemoryGoldService
from invoice_intelligence.application.services.memory_governance import (
    MemoryGovernanceService,
)
from invoice_intelligence.application.services.promotion_candidates import PromotionCandidateService
from invoice_intelligence.application.services.review_tasks import ReviewTaskService
from invoice_intelligence.application.services.training_jobs import TrainingJobService
from invoice_intelligence.application.services.transaction_analysis import (
    TransactionAnalysisService,
)
from invoice_intelligence.code_harness.application.services.code_harness import CodeHarnessService
from invoice_intelligence.code_harness.application.services.postmortem_governance import (
    PostmortemGovernanceService,
)
from invoice_intelligence.domain.governance import TrustedTenantContext


class TenantContextResolver(Protocol):
    """Resolve identity installed by trusted authentication/proxy middleware."""

    async def resolve(self, request: Request) -> TrustedTenantContext: ...


class RequestStateTenantContextResolver:
    """Reject requests unless upstream middleware installed a typed context."""

    async def resolve(self, request: Request) -> TrustedTenantContext:
        context = getattr(request.state, "trusted_tenant_context", None)
        if not isinstance(context, TrustedTenantContext):
            raise ForbiddenError("Trusted tenant context is not available")
        return context


class ApiSettings(Protocol):
    """Small settings view required by the HTTP adapter."""

    app_name: str
    api_prefix: str
    docs_enabled: bool
    metrics_endpoint_enabled: bool
    max_upload_bytes: int
    dev_tenant_id: str | None

    @property
    def environment(self) -> StrEnum: ...


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    """Application-facing objects exposed to FastAPI dependencies."""

    settings: ApiSettings
    document_ingestion_service: DocumentIngestionService
    document_access_service: DocumentAccessService
    memory_governance_service: MemoryGovernanceService
    accounting_service: AccountingService
    tenant_context_resolver: TenantContextResolver
    authorization_policy: AuthorizationPolicy
    invoice_batch_service: InvoiceBatchService | None = None
    memory_effectiveness_service: MemoryEffectivenessService | None = None
    memory_gold_service: MemoryGoldService | None = None
    auth_context_provider: AuthContextProvider | None = None
    security_audit_sink: SecurityAuditSink | None = None
    privacy_telemetry: PrivacyTelemetry | None = None
    transaction_analysis_service: TransactionAnalysisService | None = None
    promotion_candidate_service: PromotionCandidateService | None = None
    training_job_service: TrainingJobService | None = None
    evaluation_job_service: EvaluationJobService | None = None
    code_harness_service: CodeHarnessService | None = None
    code_harness_postmortem_service: PostmortemGovernanceService | None = None


def get_api_dependencies(request: Request) -> ApiDependencies:
    dependencies = getattr(request.app.state, "api_dependencies", None)
    if not isinstance(dependencies, ApiDependencies):
        raise RuntimeError("API dependencies are not initialized")
    return dependencies


ApiDependencyBundle = Annotated[ApiDependencies, Depends(get_api_dependencies)]


def get_document_ingestion_service(
    dependencies: ApiDependencyBundle,
) -> DocumentIngestionService:
    return dependencies.document_ingestion_service


def get_max_upload_bytes(dependencies: ApiDependencyBundle) -> int:
    return dependencies.settings.max_upload_bytes


def get_document_access_service(dependencies: ApiDependencyBundle) -> DocumentAccessService:
    return dependencies.document_access_service


def get_extraction_workflow_service(request: Request) -> ExtractionWorkflowService:
    service = getattr(request.app.state, "extraction_workflow_service", None)
    if not isinstance(service, ExtractionWorkflowService):
        raise RuntimeError("Extraction workflow service is not initialized")
    return service


def get_memory_governance_service(
    dependencies: ApiDependencyBundle,
) -> MemoryGovernanceService:
    return dependencies.memory_governance_service


def get_memory_effectiveness_service(
    dependencies: ApiDependencyBundle,
) -> MemoryEffectivenessService:
    if dependencies.memory_effectiveness_service is None:
        raise RuntimeError("Memory effectiveness service is not initialized")
    return dependencies.memory_effectiveness_service


def get_memory_gold_service(dependencies: ApiDependencyBundle) -> MemoryGoldService:
    if dependencies.memory_gold_service is None:
        raise ServiceUnavailableError("Gold annotation storage is not configured")
    return dependencies.memory_gold_service


def get_review_task_service(request: Request) -> ReviewTaskService:
    service = getattr(request.app.state, "review_task_service", None)
    if not isinstance(service, ReviewTaskService):
        raise RuntimeError("Review task service is not initialized")
    return service


def get_training_job_service(
    dependencies: ApiDependencyBundle,
) -> TrainingJobService:
    if dependencies.training_job_service is None:
        raise RuntimeError("Training Job service is not initialized")
    return dependencies.training_job_service


def get_evaluation_job_service(
    dependencies: ApiDependencyBundle,
) -> EvaluationJobService:
    if dependencies.evaluation_job_service is None:
        raise RuntimeError("Evaluation Job service is not initialized")
    return dependencies.evaluation_job_service


def get_code_harness_service(
    dependencies: ApiDependencyBundle,
) -> CodeHarnessService:
    if dependencies.code_harness_service is None:
        raise RuntimeError("Code Harness service is not initialized")
    return dependencies.code_harness_service


def get_code_harness_postmortem_service(
    dependencies: ApiDependencyBundle,
) -> PostmortemGovernanceService:
    if dependencies.code_harness_postmortem_service is None:
        raise RuntimeError("Code Harness postmortem service is not initialized")
    return dependencies.code_harness_postmortem_service


def get_accounting_service(dependencies: ApiDependencyBundle) -> AccountingService:
    return dependencies.accounting_service


def get_promotion_candidate_service(
    dependencies: ApiDependencyBundle,
) -> PromotionCandidateService:
    if dependencies.promotion_candidate_service is None:
        raise RuntimeError("Promotion candidate service is not initialized")
    return dependencies.promotion_candidate_service


async def get_trusted_tenant_context(
    request: Request,
    dependencies: ApiDependencyBundle,
) -> TrustedTenantContext:
    context = await dependencies.tenant_context_resolver.resolve(request)
    if context.trace_id is not None:
        request.state.server_trace_id = context.trace_id
        return context
    trace_id = getattr(request.state, "server_trace_id", None)
    if not isinstance(trace_id, str):
        trace_id = uuid4().hex
    return replace(context, trace_id=trace_id)


DocumentIngestionDependency = Annotated[
    DocumentIngestionService,
    Depends(get_document_ingestion_service),
]
DocumentAccessDependency = Annotated[
    DocumentAccessService,
    Depends(get_document_access_service),
]
MaxUploadBytesDependency = Annotated[int, Depends(get_max_upload_bytes)]
ExtractionWorkflowServiceDependency = Annotated[
    ExtractionWorkflowService,
    Depends(get_extraction_workflow_service),
]
MemoryGovernanceServiceDependency = Annotated[
    MemoryGovernanceService,
    Depends(get_memory_governance_service),
]
MemoryEffectivenessServiceDependency = Annotated[
    MemoryEffectivenessService,
    Depends(get_memory_effectiveness_service),
]
MemoryGoldServiceDependency = Annotated[MemoryGoldService, Depends(get_memory_gold_service)]
ReviewTaskServiceDependency = Annotated[
    ReviewTaskService,
    Depends(get_review_task_service),
]
TrainingJobServiceDependency = Annotated[
    TrainingJobService,
    Depends(get_training_job_service),
]
EvaluationJobServiceDependency = Annotated[
    EvaluationJobService,
    Depends(get_evaluation_job_service),
]
CodeHarnessServiceDependency = Annotated[
    CodeHarnessService,
    Depends(get_code_harness_service),
]
CodeHarnessPostmortemServiceDependency = Annotated[
    PostmortemGovernanceService,
    Depends(get_code_harness_postmortem_service),
]
AccountingServiceDependency = Annotated[
    AccountingService,
    Depends(get_accounting_service),
]
PromotionCandidateServiceDependency = Annotated[
    PromotionCandidateService, Depends(get_promotion_candidate_service)
]
TrustedTenantContextDependency = Annotated[
    TrustedTenantContext,
    Depends(get_trusted_tenant_context),
]


def get_transaction_analysis_service(
    dependencies: ApiDependencyBundle,
) -> TransactionAnalysisService:
    if dependencies.transaction_analysis_service is None:
        raise RuntimeError("Transaction analysis service is not initialized")
    return dependencies.transaction_analysis_service


TransactionAnalysisDependency = Annotated[
    TransactionAnalysisService,
    Depends(get_transaction_analysis_service),
]
