"""Executable application entrypoint and outer composition boundary."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from invoice_intelligence.api.dependencies import (
    ApiDependencies,
    RequestStateTenantContextResolver,
)
from invoice_intelligence.api.main import create_app
from invoice_intelligence.application.services.review_tasks import (
    ReviewTaskService,
    ReviewTaskServiceConfig,
)
from invoice_intelligence.bootstrap import (
    build_container,
    close_application_container,
    open_extraction_workflow_service,
)
from invoice_intelligence.config.logging import configure_logging

_container = build_container()
configure_logging(_container.settings, component="api")


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        async with open_extraction_workflow_service(_container) as service:
            app.state.extraction_workflow_service = service
            app.state.review_task_service = ReviewTaskService(
                repository=_container.review_task_repository,
                workflow_service=service,
                config=ReviewTaskServiceConfig(
                    default_lease_seconds=(_container.settings.review_task_default_lease_seconds),
                    maximum_lease_seconds=(_container.settings.review_task_maximum_lease_seconds),
                ),
            )
            yield
    finally:
        await close_application_container(_container)


app = create_app(
    ApiDependencies(
        settings=_container.settings,
        document_ingestion_service=_container.document_ingestion_service,
        document_access_service=_container.document_access_service,
        memory_governance_service=_container.memory_governance_service,
        accounting_service=_container.accounting_service,
        promotion_candidate_service=_container.promotion_candidate_service,
        tenant_context_resolver=RequestStateTenantContextResolver(),
        authorization_policy=_container.authorization_policy,
        auth_context_provider=_container.auth_context_provider,
        security_audit_sink=_container.security_audit_sink,
        privacy_telemetry=_container.privacy_telemetry,
        transaction_analysis_service=_container.transaction_analysis_service,
        training_job_service=_container.training_job_service,
        evaluation_job_service=_container.evaluation_job_service,
        code_harness_service=_container.code_harness_service,
        code_harness_postmortem_service=_container.code_harness_postmortem_service,
    ),
    lifespan=_lifespan,
)
