"""仅从已完成的持久化提取结果派生交易候选。"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from pydantic import ValidationError

from invoice_intelligence.application.errors import (
    IdempotencyInProgressError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from invoice_intelligence.application.ports.business_persistence import (
    BusinessQueryRepository,
    IdempotencyRepository,
    IdempotencyStatus,
)
from invoice_intelligence.application.ports.transaction_rules import (
    TransactionAnalysisRepository,
    TransactionRuleEngine,
)
from invoice_intelligence.application.services.duplicate_fingerprint import (
    DuplicateFingerprintService,
)
from invoice_intelligence.application.services.idempotency import normalize_idempotency_key
from invoice_intelligence.domain.governance import TrustedTenantContext
from invoice_intelligence.domain.invoice import InvoiceExtraction
from invoice_intelligence.domain.transactions import (
    DuplicateTransactionCase,
    RiskAssessment,
    RiskStatus,
    TransactionCandidate,
    TransactionReviewStatus,
)
from invoice_intelligence.domain.workflow import WorkflowStatus


class TransactionAnalysisService:
    def __init__(
        self,
        repository: TransactionAnalysisRepository,
        rule_engine: TransactionRuleEngine,
        business_repository: BusinessQueryRepository,
        idempotency_repository: IdempotencyRepository,
        schema_version: str,
    ) -> None:
        self._repository = repository
        self._rule_engine = rule_engine
        self._business = business_repository
        self._idempotency = idempotency_repository
        self._schema_version = schema_version
        self._fingerprint = DuplicateFingerprintService()

    async def analyze(
        self, context: TrustedTenantContext, *, run_id: str, idempotency_key: str
    ) -> RiskAssessment:
        candidate = await self._candidate_from_source(context, run_id)
        candidate_id = candidate.candidate_id
        key = normalize_idempotency_key(idempotency_key)
        operation = f"transaction.analyze.{context.tenant_id}"
        request_hash = self._digest(
            {
                "run_id": run_id,
                "candidate": candidate.model_dump(mode="json", exclude={"created_at"}),
            }
        )
        key_hash = self._digest({"operation": operation, "key": key})
        claim_id = uuid4().hex
        claim = await self._idempotency.claim_idempotency(
            operation, key, request_hash, claim_id
        )
        if claim.status == IdempotencyStatus.COMPLETED:
            assert claim.response_payload is not None
            return RiskAssessment.model_validate(claim.response_payload)
        recovered = await self._repository.get_audit_result(context.tenant_id, key_hash)
        if recovered is not None:
            await self._idempotency.complete_idempotency(
                operation, key, request_hash, recovered.model_dump(mode="json")
            )
            return recovered
        try:
            stored = await self._repository.get_candidate(context.tenant_id, candidate_id)
            if stored is not None and (
                stored.model_dump(exclude={"created_at"})
                != candidate.model_dump(exclude={"created_at"})
            ):
                raise ResourceConflictError("Existing transaction candidate has a different source")
            current = await self._repository.get_assessment(context.tenant_id, candidate_id)
            if current is not None:
                assessment = current
            elif claim.resource_id != claim_id:
                raise IdempotencyInProgressError("Transaction analysis is in progress")
            else:
                assessment = await self._assess(candidate, key_hash)
        except Exception:
            if claim.resource_id == claim_id:
                await self._idempotency.release_idempotency(
                    operation, key, request_hash, claim_id
                )
            raise
        await self._idempotency.complete_idempotency(
            operation, key, request_hash, assessment.model_dump(mode="json")
        )
        return assessment

    async def _candidate_from_source(
        self, context: TrustedTenantContext, run_id: str
    ) -> TransactionCandidate:
        run = await self._business.get_run(run_id, context.tenant_id)
        result = await self._business.get_result(run_id, context.tenant_id)
        if (
            run is None
            or run.status != WorkflowStatus.COMPLETED
            or result is None
            or result.document_id != run.identity.document_id
        ):
            raise ResourceNotFoundError("Completed transaction source was not found")
        try:
            invoice = InvoiceExtraction.model_validate(result.payload.get("invoice"))
        except ValidationError as exc:
            raise ResourceConflictError("Persisted invoice cannot be analyzed") from exc

        def opaque(value: str | None) -> str | None:
            return (
                sha256(f"{context.tenant_id}|{value.strip().casefold()}".encode()).hexdigest()
                if value and value.strip()
                else None
            )

        candidate_id = sha256(f"{context.tenant_id}|{run_id}".encode()).hexdigest()
        vendor_key = opaque(invoice.seller_name)
        reference = opaque(invoice.invoice_number)
        return TransactionCandidate(
            candidate_id=candidate_id,
            tenant_id=context.tenant_id,
            run_id=run_id,
            document_id=result.document_id,
            fingerprint=self._fingerprint.create(
                tenant_id=context.tenant_id,
                vendor_key=vendor_key,
                transaction_date=invoice.invoice_date,
                amount=invoice.invoice_total_tax_price,
                currency=invoice.currency,
                reference=reference,
            ),
            vendor_key=vendor_key,
            transaction_date=invoice.invoice_date,
            amount=invoice.invoice_total_tax_price,
            currency=invoice.currency,
            normalized_reference=reference,
            schema_version=self._schema_version,
            created_at=datetime.now(UTC),
        )

    async def _assess(self, candidate: TransactionCandidate, key_hash: str) -> RiskAssessment:
        classification = self._rule_engine.classify(candidate)
        matched = await self._repository.find_duplicate(candidate)
        duplicate = None
        if matched is not None:
            duplicate = DuplicateTransactionCase(
                case_id=uuid4().hex,
                tenant_id=candidate.tenant_id,
                candidate_transaction_id=candidate.candidate_id,
                matched_transaction_id=matched.candidate_id,
                matching_dimensions=("fingerprint",),
                time_window_days=0,
                amount_tolerance=Decimal(0),
                vendor_similarity=Decimal(1),
                evidence=({"type": "stable_fingerprint_match"},),
                idempotency_key_hash=key_hash,
                created_at=datetime.now(UTC),
            )
        duplicate, suspicious, assessment = self._rule_engine.assess_risk(candidate, duplicate)
        classification = classification.model_copy(update={"idempotency_key_hash": key_hash})
        if duplicate is not None:
            duplicate = duplicate.model_copy(update={"idempotency_key_hash": key_hash})
        if suspicious is not None:
            suspicious = suspicious.model_copy(update={"idempotency_key_hash": key_hash})
        assessment = assessment.model_copy(update={"idempotency_key_hash": key_hash})
        return await self._repository.save_analysis(
            candidate, classification, duplicate, suspicious, assessment
        )

    async def review(
        self,
        context: TrustedTenantContext,
        *,
        candidate_id: str,
        decision: TransactionReviewStatus,
        expected_revision: int,
        idempotency_key: str,
    ) -> RiskAssessment:
        current = await self._repository.get_assessment(context.tenant_id, candidate_id)
        if current is None:
            raise ResourceNotFoundError("Transaction assessment was not found")
        stored = await self._repository.get_candidate(context.tenant_id, candidate_id)
        if stored is None:
            raise ResourceNotFoundError("Transaction candidate was not found")
        try:
            derived = await self._candidate_from_source(context, stored.run_id)
        except (ResourceNotFoundError, ResourceConflictError):
            derived = None
        source_trusted = derived is not None and (
            stored.model_dump(exclude={"created_at"})
            == derived.model_dump(exclude={"created_at"})
        )
        if not source_trusted and decision != TransactionReviewStatus.ESCALATED:
            raise ResourceConflictError("Transaction candidate source is not trusted")
        if decision == TransactionReviewStatus.PENDING:
            raise ResourceConflictError("Pending is not a review decision")
        key = normalize_idempotency_key(idempotency_key)
        operation = f"transaction.review.{context.tenant_id}"
        request_hash = self._digest(
            {
                "candidate_id": candidate_id,
                "decision": decision.value,
                "expected_revision": expected_revision,
                "reviewer_id": context.actor_id,
            }
        )
        key_hash = self._digest({"operation": operation, "key": key})
        claim_id = uuid4().hex
        claim = await self._idempotency.claim_idempotency(
            operation, key, request_hash, claim_id
        )
        if claim.status == IdempotencyStatus.COMPLETED:
            assert claim.response_payload is not None
            return RiskAssessment.model_validate(claim.response_payload)
        recovered = await self._repository.get_audit_result(context.tenant_id, key_hash)
        if recovered is not None:
            await self._idempotency.complete_idempotency(
                operation, key, request_hash, recovered.model_dump(mode="json")
            )
            return recovered
        if claim.resource_id != claim_id:
            raise IdempotencyInProgressError("Transaction review is in progress")
        try:
            if current.revision != expected_revision:
                raise ResourceConflictError("Transaction assessment revision changed")
            updated = current.model_copy(
                update={
                    "status": RiskStatus(decision.value),
                    "reviewer_id": context.actor_id,
                    "reviewer_decision": decision,
                    "evidence": (
                        (*current.evidence, {"type": "legacy_candidate_source_untrusted"})
                        if not source_trusted else current.evidence
                    ),
                    "idempotency_key_hash": key_hash,
                    "revision": expected_revision + 1,
                }
            )
            saved = await self._repository.save_review(updated, expected_revision)
        except Exception:
            await self._idempotency.release_idempotency(operation, key, request_hash, claim_id)
            raise
        await self._idempotency.complete_idempotency(
            operation, key, request_hash, saved.model_dump(mode="json")
        )
        return saved

    async def process_pending(self, limit: int = 25) -> int:
        candidates = await self._repository.list_pending_candidates(limit)
        for candidate in candidates:
            context = TrustedTenantContext(
                tenant_id=candidate.tenant_id,
                actor_id="transaction-worker",
                permissions=frozenset(),
            )
            await self.analyze(
                context, run_id=candidate.run_id, idempotency_key=f"worker:{candidate.candidate_id}"
            )
        return len(candidates)

    @staticmethod
    def _digest(value: dict[str, object]) -> str:
        return sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
