"""FastAPI adapter for tenant-scoped field semantic governance."""

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query

from invoice_intelligence.api.dependencies import (
    MemoryGovernanceServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.presenters import (
    present_field_alias_decision,
    present_field_semantic_conflicts,
    present_field_semantic_index,
    present_field_semantic_projection_execution,
    present_field_semantics,
    present_memory_conflict_resolution,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.memory import (
    FieldAliasDecisionRequest,
    FieldAliasDecisionResponse,
    FieldSemanticConflictListResponse,
    FieldSemanticIndexRebuildRequest,
    FieldSemanticIndexResponse,
    FieldSemanticListResponse,
    FieldSemanticProjectionExecutionResponse,
    IndexProjectionRequest,
    MemoryConflictResolutionRequest,
    MemoryConflictResolutionResultResponse,
)
from invoice_intelligence.domain.admission import MemoryConflictStatus
from invoice_intelligence.domain.field_semantics import FieldAliasStatus

router = APIRouter(prefix="/field-semantics", tags=["field-semantic-governance"])


@router.post(
    "/indexes/rebuild",
    response_model=FieldSemanticIndexResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def register_field_semantic_index(
    request: FieldSemanticIndexRebuildRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> FieldSemanticIndexResponse:
    return present_field_semantic_index(
        await service.register_field_semantic_index(
            context,
            index_version=request.index_version,
            schema_version=request.schema_version,
            catalog_version=request.catalog_version,
            dense_model_version=request.dense_model_version,
            sparse_model_version=request.sparse_model_version,
        )
    )


@router.get(
    "/indexes/{index_version}",
    response_model=FieldSemanticIndexResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_field_semantic_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> FieldSemanticIndexResponse:
    return present_field_semantic_index(
        await service.get_field_semantic_index(context, index_version)
    )


@router.post(
    "/indexes/{index_version}/project",
    response_model=FieldSemanticProjectionExecutionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def project_field_semantic_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    request: IndexProjectionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> FieldSemanticProjectionExecutionResponse:
    return present_field_semantic_projection_execution(
        await service.project_field_semantic_index(
            context,
            index_version,
            limit=request.limit,
        )
    )


@router.post(
    "/indexes/{index_version}/activate",
    response_model=FieldSemanticIndexResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def activate_field_semantic_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> FieldSemanticIndexResponse:
    return present_field_semantic_index(
        await service.activate_field_semantic_index(context, index_version)
    )


@router.post("/indexes/{index_version}/rollback", response_model=FieldSemanticIndexResponse, responses=STANDARD_ERROR_RESPONSES)
async def rollback_field_semantic_index(
    index_version: Annotated[str, Path(min_length=1, max_length=256)],
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
) -> FieldSemanticIndexResponse:
    return present_field_semantic_index(
        await service.rollback_field_semantic_index(context, index_version)
    )


@router.get(
    "",
    response_model=FieldSemanticListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_field_semantics(
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    document_type: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    canonical_field_path: Annotated[
        str | None,
        Query(min_length=1, max_length=512),
    ] = None,
    catalog_version: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
    alias_statuses: Annotated[
        list[FieldAliasStatus] | None,
        Query(alias="alias_status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> FieldSemanticListResponse:
    return present_field_semantics(
        await service.list_field_semantics(
            context,
            document_type=document_type,
            canonical_field_path=canonical_field_path,
            catalog_version=catalog_version,
            alias_statuses=tuple(alias_statuses or tuple(FieldAliasStatus)),
            limit=limit,
            cursor=cursor,
        )
    )


@router.get(
    "/conflicts",
    response_model=FieldSemanticConflictListResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_field_semantic_conflicts(
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    conflict_statuses: Annotated[
        list[MemoryConflictStatus] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    field_path: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> FieldSemanticConflictListResponse:
    return present_field_semantic_conflicts(
        await service.list_field_semantic_conflicts(
            context,
            statuses=tuple(conflict_statuses or (MemoryConflictStatus.OPEN,)),
            limit=limit,
            cursor=cursor,
            field_path=field_path,
        )
    )


@router.post(
    "/conflicts/{conflict_id}/resolve",
    response_model=MemoryConflictResolutionResultResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def resolve_field_semantic_conflict(
    conflict_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: MemoryConflictResolutionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryConflictResolutionResultResponse:
    return present_memory_conflict_resolution(
        await service.resolve_field_semantic_conflict(
            context,
            conflict_id,
            expected_status=request.expected_status,
            reason=request.reason,
            selected_canonical_field_path=(
                request.selected_canonical_field_path
            ),
            resolution_note=request.resolution_note,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/conflicts/{conflict_id}/dismiss",
    response_model=MemoryConflictResolutionResultResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def dismiss_field_semantic_conflict(
    conflict_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: MemoryConflictResolutionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MemoryConflictResolutionResultResponse:
    return present_memory_conflict_resolution(
        await service.dismiss_field_semantic_conflict(
            context,
            conflict_id,
            expected_status=request.expected_status,
            reason=request.reason,
            selected_canonical_field_path=(
                request.selected_canonical_field_path
            ),
            resolution_note=request.resolution_note,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/aliases/{alias_id}/approve",
    response_model=FieldAliasDecisionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def approve_field_alias(
    alias_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: FieldAliasDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FieldAliasDecisionResponse:
    return present_field_alias_decision(
        await service.approve_field_alias(
            context,
            alias_id,
            request.reason,
            request.expected_revision,
            idempotency_key,
        )
    )


@router.post(
    "/aliases/{alias_id}/disable",
    response_model=FieldAliasDecisionResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def disable_field_alias(
    alias_id: Annotated[str, Path(min_length=1, max_length=64)],
    request: FieldAliasDecisionRequest,
    service: MemoryGovernanceServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> FieldAliasDecisionResponse:
    return present_field_alias_decision(
        await service.disable_field_alias(
            context,
            alias_id,
            request.reason,
            request.expected_revision,
            idempotency_key,
        )
    )
