"""Read-only memory operation and measured benefit endpoints."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query

from invoice_intelligence.api.dependencies import (
    MemoryEffectivenessServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.api.schemas.memory_effectiveness import (
    EffectivenessOverviewResponse,
    EffectivenessRunResponse,
    EffectivenessScenariosResponse,
    EffectivenessStagesResponse,
)

router = APIRouter(prefix="/memory/effectiveness", tags=["memory-effectiveness"])


@router.get("/overview", response_model=EffectivenessOverviewResponse,
            responses=STANDARD_ERROR_RESPONSES)
async def overview(
    service: MemoryEffectivenessServiceDependency,
    context: TrustedTenantContextDependency,
    since: datetime | None = None,
    until: datetime | None = None,
) -> EffectivenessOverviewResponse:
    return EffectivenessOverviewResponse.model_validate(
        await service.overview(context, since=since, until=until)
    )


@router.get("/stages", response_model=EffectivenessStagesResponse,
            responses=STANDARD_ERROR_RESPONSES)
async def stages(
    service: MemoryEffectivenessServiceDependency,
    context: TrustedTenantContextDependency,
    since: datetime | None = None,
    until: datetime | None = None,
) -> EffectivenessStagesResponse:
    return EffectivenessStagesResponse.model_validate(
        await service.stages(context, since=since, until=until)
    )


@router.get("/scenarios", response_model=EffectivenessScenariosResponse,
            responses=STANDARD_ERROR_RESPONSES)
async def scenarios(
    service: MemoryEffectivenessServiceDependency,
    context: TrustedTenantContextDependency,
    run_id: Annotated[str | None, Query(max_length=64)] = None,
    field_path: Annotated[str | None, Query(max_length=128)] = None,
) -> EffectivenessScenariosResponse:
    return EffectivenessScenariosResponse.model_validate(
        await service.scenarios(context, run_id=run_id, field_path=field_path)
    )


@router.get("/runs/{run_id}", response_model=EffectivenessRunResponse,
            responses=STANDARD_ERROR_RESPONSES)
async def run(
    run_id: Annotated[str, Path(min_length=1, max_length=64)],
    service: MemoryEffectivenessServiceDependency,
    context: TrustedTenantContextDependency,
) -> EffectivenessRunResponse:
    return EffectivenessRunResponse.model_validate(await service.run(context, run_id))
