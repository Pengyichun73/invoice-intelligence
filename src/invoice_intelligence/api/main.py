"""FastAPI application factory."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from invoice_intelligence.api.dependencies import ApiDependencies
from invoice_intelligence.api.errors import install_exception_handlers
from invoice_intelligence.api.routes.accounting import router as accounting_router
from invoice_intelligence.api.routes.documents import router as documents_router
from invoice_intelligence.api.routes.invoice_batches import router as invoice_batches_router
from invoice_intelligence.api.routes.evaluations import router as evaluations_router
from invoice_intelligence.api.routes.field_semantics import router as field_semantics_router
from invoice_intelligence.api.routes.health import router as health_router
from invoice_intelligence.api.routes.memory import router as memory_router
from invoice_intelligence.api.routes.memory_effectiveness import (
    router as memory_effectiveness_router,
)
from invoice_intelligence.api.routes.memory_gold import router as memory_gold_router
from invoice_intelligence.api.routes.promotion import router as promotion_router
from invoice_intelligence.api.routes.reviews import router as reviews_router
from invoice_intelligence.api.routes.runs import router as runs_router
from invoice_intelligence.api.routes.training import router as training_router
from invoice_intelligence.api.routes.transactions import router as transactions_router
from invoice_intelligence.api.routes.code_harness import router as code_harness_router
from invoice_intelligence.api.routes.code_harness_postmortems import (
    router as code_harness_postmortems_router,
)
from invoice_intelligence.api.security import enforce_request_security
from invoice_intelligence.application.ports.observability import TraceStage
from invoice_intelligence.domain.governance import TrustedTenantContext

AppLifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    dependencies: ApiDependencies,
    lifespan: AppLifespan,
) -> FastAPI:
    """Create a FastAPI application with explicitly injected dependencies."""

    settings = dependencies.settings
    app = FastAPI(
        title=settings.app_name,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.api_dependencies = dependencies

    @app.middleware("http")
    async def install_server_trace(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_started_at = perf_counter()
        trusted = getattr(request.state, "trusted_tenant_context", None)
        trace_id = (
            trusted.trace_id
            if isinstance(trusted, TrustedTenantContext) and trusted.trace_id is not None
            else uuid4().hex
        )
        request.state.server_trace_id = trace_id
        telemetry = dependencies.privacy_telemetry
        is_governance_write = request.method not in {"GET", "HEAD", "OPTIONS"} and (
            request.url.path.startswith(f"{settings.api_prefix}/memory")
            or request.url.path.startswith(f"{settings.api_prefix}/accounting")
            or request.url.path.startswith(f"{settings.api_prefix}/field-semantics")
        )
        if telemetry is not None and is_governance_write:
            tenant_id = (
                trusted.tenant_id if isinstance(trusted, TrustedTenantContext) else "unknown"
            )
            with telemetry.span(
                trace_id=trace_id,
                tenant_id=tenant_id,
                stage=TraceStage.GOVERNANCE_OPERATION,
                operation="http_governance_write",
                attributes={"resource_type": request.url.path[:256]},
            ):
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["X-Trace-ID"] = getattr(
            request.state,
            "server_trace_id",
            trace_id,
        )
        return response

    @app.middleware("http")
    async def install_trusted_auth_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        return await enforce_request_security(request, call_next, dependencies)

    install_exception_handlers(app)
    app.include_router(health_router, prefix=settings.api_prefix)
    app.include_router(documents_router, prefix=settings.api_prefix)
    app.include_router(invoice_batches_router, prefix=settings.api_prefix)
    app.include_router(runs_router, prefix=settings.api_prefix)
    app.include_router(reviews_router, prefix=settings.api_prefix)
    app.include_router(memory_router, prefix=settings.api_prefix)
    app.include_router(memory_effectiveness_router, prefix=settings.api_prefix)
    app.include_router(memory_gold_router, prefix=settings.api_prefix)
    app.include_router(field_semantics_router, prefix=settings.api_prefix)
    app.include_router(transactions_router, prefix=settings.api_prefix)
    app.include_router(accounting_router, prefix=settings.api_prefix)
    app.include_router(promotion_router, prefix=settings.api_prefix)
    app.include_router(training_router, prefix=settings.api_prefix)
    app.include_router(evaluations_router, prefix=settings.api_prefix)
    app.include_router(code_harness_router, prefix=settings.api_prefix)
    app.include_router(code_harness_postmortems_router, prefix=settings.api_prefix)
    return app
