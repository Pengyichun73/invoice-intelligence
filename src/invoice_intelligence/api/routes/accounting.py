"""Tenant-scoped accounting protocol adapter."""

from typing import Annotated

from fastapi import APIRouter, Header, Path

from invoice_intelligence.api.dependencies import (
    AccountingServiceDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.accounting import (
    AccountingAuditResponse,
    AccountingCandidateResponse,
    ApproveAccountingCandidateRequest,
    CreateAccountingCandidateRequest,
    ExchangeRateRequest,
    ExchangeRateResponse,
    PostAccountingCandidateRequest,
    RejectAccountingCandidateRequest,
)
from invoice_intelligence.api.schemas.common import STANDARD_ERROR_RESPONSES
from invoice_intelligence.domain.accounting import (
    AccountingAdvisory,
    AccountingCandidate,
    PostingLine,
)

router = APIRouter(prefix="/accounting", tags=["accounting"])


@router.post(
    "/candidates", response_model=AccountingCandidateResponse, responses=STANDARD_ERROR_RESPONSES
)
async def create_candidate(
    request: CreateAccountingCandidateRequest,
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> AccountingCandidateResponse:
    return _candidate(
        await service.create_candidate(
            context, run_id=request.run_id, idempotency_key=idempotency_key
        )
    )


@router.get(
    "/candidates/{candidate_id}",
    response_model=AccountingCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def get_candidate(
    candidate_id: Annotated[str, Path(min_length=1)],
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
) -> AccountingCandidateResponse:
    return _candidate(await service.get_candidate(context, candidate_id))


@router.post(
    "/candidates/{candidate_id}/approve",
    response_model=AccountingCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def approve_candidate(
    candidate_id: Annotated[str, Path(min_length=1)],
    request: ApproveAccountingCandidateRequest,
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> AccountingCandidateResponse:
    return _candidate(
        await service.approve(
            context,
            candidate_id=candidate_id,
            expected_revision=request.expected_revision,
            tax_rule_version=request.tax_rule_version,
            chart_of_accounts_version=request.chart_of_accounts_version,
            posting_rule_version=request.posting_rule_version,
            tax_code=request.tax_code,
            taxable_amount=request.taxable_amount,
            tax_amount=request.tax_amount,
            advisory=(
                AccountingAdvisory(**request.advisory.model_dump())
                if request.advisory is not None
                else None
            ),
            lines=tuple(PostingLine(**line.model_dump()) for line in request.lines),
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/candidates/{candidate_id}/reject",
    response_model=AccountingCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def reject_candidate(
    candidate_id: Annotated[str, Path(min_length=1)],
    request: RejectAccountingCandidateRequest,
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> AccountingCandidateResponse:
    return _candidate(
        await service.reject(
            context,
            candidate_id=candidate_id,
            expected_revision=request.expected_revision,
            reason_code=request.reason_code,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/candidates/{candidate_id}/post",
    response_model=AccountingCandidateResponse,
    responses=STANDARD_ERROR_RESPONSES,
)
async def post_candidate(
    candidate_id: Annotated[str, Path(min_length=1)],
    request: PostAccountingCandidateRequest,
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> AccountingCandidateResponse:
    return _candidate(
        await service.post(
            context,
            candidate_id=candidate_id,
            expected_revision=request.expected_revision,
            exchange_rate=None,
            idempotency_key=idempotency_key,
        )
    )


@router.post(
    "/exchange-rates", response_model=ExchangeRateResponse, responses=STANDARD_ERROR_RESPONSES
)
async def snapshot_exchange_rate(
    request: ExchangeRateRequest,
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1)],
) -> ExchangeRateResponse:
    value = await service.snapshot_exchange_rate(
        context,
        base_currency=request.base_currency,
        quote_currency=request.quote_currency,
        effective_at=request.effective_at,
        idempotency_key=idempotency_key,
    )
    return ExchangeRateResponse(
        source=value.source,
        quote_currency=value.quote_currency,
        base_currency=value.base_currency,
        rate=value.rate,
        precision=value.precision,
        effective_at=value.effective_at,
        snapshot_id=value.snapshot_id,
    )


@router.get(
    "/candidates/{candidate_id}/audit",
    response_model=tuple[AccountingAuditResponse, ...],
    responses=STANDARD_ERROR_RESPONSES,
)
async def list_audit(
    candidate_id: Annotated[str, Path(min_length=1)],
    service: AccountingServiceDependency,
    context: TrustedTenantContextDependency,
) -> tuple[AccountingAuditResponse, ...]:
    return tuple(
        AccountingAuditResponse(
            audit_id=item.audit_id,
            candidate_id=item.candidate_id,
            action=item.action,
            actor_id=item.actor_id,
            from_status=item.from_status,
            to_status=item.to_status,
            revision=item.revision,
            reason_code=item.reason_code,
            trace_id=item.trace_id,
            created_at=item.created_at,
        )
        for item in await service.list_audit(context, candidate_id)
    )


def _candidate(value: AccountingCandidate) -> AccountingCandidateResponse:
    return AccountingCandidateResponse(
        candidate_id=value.candidate_id,
        run_id=value.run_id,
        status=value.status,
        tax_rule_version=value.tax_rule_version,
        chart_of_accounts_version=value.chart_of_accounts_version,
        posting_rule_version=value.posting_rule_version,
        revision=value.revision,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )
