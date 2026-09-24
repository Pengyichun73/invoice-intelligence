from fastapi import APIRouter, Header, status

from invoice_intelligence.api.dependencies import (
    TransactionAnalysisDependency,
    TrustedTenantContextDependency,
)
from invoice_intelligence.api.schemas.transactions import (
    TransactionAssessmentResponse,
    TransactionCandidateRequest,
    TransactionReviewRequest,
)

router = APIRouter(prefix="/transactions", tags=["transaction-analysis"])


@router.post(
    "/candidates/analyze",
    response_model=TransactionAssessmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def analyze_candidate(
    payload: TransactionCandidateRequest,
    context: TrustedTenantContextDependency,
    service: TransactionAnalysisDependency,
    idempotency_key: str = Header(..., min_length=8, max_length=256),
) -> TransactionAssessmentResponse:
    result = await service.analyze(context, run_id=payload.run_id, idempotency_key=idempotency_key)
    return TransactionAssessmentResponse.from_domain(result)


@router.post(
    "/candidates/{candidate_id}/review",
    response_model=TransactionAssessmentResponse,
)
async def review_candidate(
    candidate_id: str,
    payload: TransactionReviewRequest,
    context: TrustedTenantContextDependency,
    service: TransactionAnalysisDependency,
    idempotency_key: str = Header(..., min_length=8, max_length=256),
) -> TransactionAssessmentResponse:
    result = await service.review(
        context,
        candidate_id=candidate_id,
        decision=payload.decision,
        expected_revision=payload.expected_revision,
        idempotency_key=idempotency_key,
    )
    return TransactionAssessmentResponse.from_domain(result)
