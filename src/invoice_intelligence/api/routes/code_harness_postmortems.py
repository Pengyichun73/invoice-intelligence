"""FastAPI adapter for bounded Harness postmortem governance."""

from typing import Annotated

from fastapi import APIRouter, Path

from invoice_intelligence.api.dependencies import (
    CodeHarnessPostmortemServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.code_harness_postmortem import (
    PostmortemAdmissionRequest,
    PostmortemResponse,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.code_harness.domain.postmortem import Postmortem
from invoice_intelligence.code_harness.domain.tenant_boundary import HarnessTenantContext
from invoice_intelligence.domain.governance import TrustedTenantContext

router = APIRouter(prefix="/code-harness/postmortems", tags=["code-harness"])


@router.get(
    "/{postmortem_id}",
    response_model=PostmortemResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_postmortem(
    postmortem_id: Annotated[str, Path(min_length=1, max_length=128)],
    service: CodeHarnessPostmortemServiceDependency,
    context: TrustedTenantContextDependency,
) -> PostmortemResponse:
    return _response(
        await service.get(
            context=_context(context),
            postmortem_id=postmortem_id,
        )
    )


@router.post(
    "/{postmortem_id}/admission",
    response_model=PostmortemResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def change_postmortem_admission(
    postmortem_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: PostmortemAdmissionRequest,
    service: CodeHarnessPostmortemServiceDependency,
    context: TrustedTenantContextDependency,
) -> PostmortemResponse:
    return _response(
        await service.change_admission(
            context=_context(context),
            postmortem_id=postmortem_id,
            expected_revision=request.expected_revision,
            status=request.status,
            reason_code=request.reason_code,
        )
    )


def _context(context: TrustedTenantContext) -> HarnessTenantContext:
    return HarnessTenantContext(
        tenant_id=context.tenant_id,
        actor_id=context.actor_id,
        trace_id=context.trace_id,
    )


def _response(postmortem: Postmortem) -> PostmortemResponse:
    return PostmortemResponse(
        postmortem_id=postmortem.postmortem_id,
        fingerprint=postmortem.fingerprint,
        version_scope=postmortem.version_scope,
        occurrence_count=postmortem.occurrence_count,
        admission_status=postmortem.admission_status,
        source_event_count=len(postmortem.source_event_ids),
        created_at=postmortem.created_at,
        updated_at=postmortem.updated_at,
        error_signature=postmortem.error_signature,
        root_cause=postmortem.root_cause,
        solution_pattern=postmortem.solution_pattern,
        affected_language=postmortem.affected_language,
        affected_symbol_kind=postmortem.affected_symbol_kind,
        patch_shape=postmortem.patch_shape,
        source_trace_id=postmortem.source_trace_id,
        revision=postmortem.revision,
    )
