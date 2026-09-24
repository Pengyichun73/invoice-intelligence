"""Accounting use cases; extraction can create candidates but never postings."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import uuid4

from invoice_intelligence.application.errors import (
    IdempotencyInProgressError,
    ResourceNotFoundError,
    ServiceUnavailableError,
    UnprocessableEntityError,
)
from invoice_intelligence.application.ports.accounting import (
    AccountingPostingProvider,
    AccountingRepository,
    ExchangeRateProvider,
)
from invoice_intelligence.application.ports.business_persistence import (
    IdempotencyRepository,
    IdempotencyStatus,
)
from invoice_intelligence.application.services.idempotency import normalize_idempotency_key
from invoice_intelligence.domain.accounting import (
    AccountingAdvisory,
    AccountingAuditEvent,
    AccountingCandidate,
    ExchangeRateSnapshot,
    PostingLine,
)
from invoice_intelligence.domain.governance import TrustedTenantContext


class AccountingService:
    def __init__(
        self,
        *,
        repository: AccountingRepository,
        idempotency_repository: IdempotencyRepository,
        exchange_rate_provider: ExchangeRateProvider,
        posting_provider: AccountingPostingProvider,
    ) -> None:
        self._repository = repository
        self._idempotency = idempotency_repository
        self._exchange_rates = exchange_rate_provider
        self._posting = posting_provider

    async def create_candidate(
        self, context: TrustedTenantContext, *, run_id: str, idempotency_key: str
    ) -> AccountingCandidate:
        payload = {"run_id": run_id}

        async def action() -> AccountingCandidate:
            return await self._repository.create_candidate(
                tenant_id=context.tenant_id,
                run_id=run_id,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
            )

        return await self._candidate_write(
            context, "create", idempotency_key, payload, run_id, action
        )

    async def get_candidate(
        self, context: TrustedTenantContext, candidate_id: str
    ) -> AccountingCandidate:
        candidate = await self._repository.get_candidate(candidate_id, context.tenant_id)
        if candidate is None:
            raise ResourceNotFoundError("Accounting candidate was not found")
        return candidate

    async def approve(
        self,
        context: TrustedTenantContext,
        *,
        candidate_id: str,
        expected_revision: int,
        tax_rule_version: str,
        chart_of_accounts_version: str,
        posting_rule_version: str,
        tax_code: str,
        taxable_amount: Decimal,
        tax_amount: Decimal,
        advisory: AccountingAdvisory | None,
        lines: tuple[PostingLine, ...],
        idempotency_key: str,
    ) -> AccountingCandidate:
        if not tax_rule_version or not chart_of_accounts_version or not posting_rule_version:
            raise UnprocessableEntityError("All accounting rule versions are required")
        if not lines or sum(
            (line.amount for line in lines if line.side == "debit"), Decimal()
        ) != sum((line.amount for line in lines if line.side == "credit"), Decimal()):
            raise UnprocessableEntityError(
                "Posting proposal must contain balanced debit and credit lines"
            )
        payload = {
            "candidate_id": candidate_id,
            "expected_revision": expected_revision,
            "tax_rule_version": tax_rule_version,
            "chart_of_accounts_version": chart_of_accounts_version,
            "posting_rule_version": posting_rule_version,
            "tax_code": tax_code,
            "taxable_amount": str(taxable_amount),
            "tax_amount": str(tax_amount),
            "advisory": asdict(advisory) if advisory is not None else None,
            "lines": [asdict(line) | {"amount": str(line.amount)} for line in lines],
        }

        async def action() -> AccountingCandidate:
            return await self._repository.approve(
                candidate_id=candidate_id,
                tenant_id=context.tenant_id,
                expected_revision=expected_revision,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
                tax_rule_version=tax_rule_version,
                chart_of_accounts_version=chart_of_accounts_version,
                posting_rule_version=posting_rule_version,
                tax_code=tax_code,
                taxable_amount=taxable_amount,
                tax_amount=tax_amount,
                advisory=advisory,
                lines=lines,
            )

        return await self._candidate_write(
            context, "approve", idempotency_key, payload, candidate_id, action
        )

    async def reject(
        self,
        context: TrustedTenantContext,
        *,
        candidate_id: str,
        expected_revision: int,
        reason_code: str,
        idempotency_key: str,
    ) -> AccountingCandidate:
        payload = {
            "candidate_id": candidate_id,
            "expected_revision": expected_revision,
            "reason_code": reason_code,
        }

        async def action() -> AccountingCandidate:
            return await self._repository.reject(
                candidate_id=candidate_id,
                tenant_id=context.tenant_id,
                expected_revision=expected_revision,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
                reason_code=reason_code,
            )

        return await self._candidate_write(
            context, "reject", idempotency_key, payload, candidate_id, action
        )

    async def snapshot_exchange_rate(
        self,
        context: TrustedTenantContext,
        *,
        base_currency: str,
        quote_currency: str,
        effective_at: datetime,
        idempotency_key: str,
    ) -> ExchangeRateSnapshot:
        key = normalize_idempotency_key(idempotency_key)
        payload = {
            "base_currency": base_currency,
            "quote_currency": quote_currency,
            "effective_at": effective_at.isoformat(),
        }
        operation, request_hash = self._binding(context, "exchange_rate", payload)
        claim_resource_id = uuid4().hex
        claim = await self._idempotency.claim_idempotency(
            operation, key, request_hash, claim_resource_id
        )
        if claim.status == IdempotencyStatus.COMPLETED:
            assert claim.response_payload is not None
            return _snapshot_from_payload(claim.response_payload)
        if claim.resource_id != claim_resource_id:
            raise IdempotencyInProgressError("Exchange-rate request is already in progress")
        snapshot = await self._exchange_rates.get_rate(
            base_currency=base_currency, quote_currency=quote_currency, effective_at=effective_at
        )
        snapshot = await self._repository.save_exchange_rate(context.tenant_id, snapshot)
        response = _snapshot_payload(snapshot)
        await self._idempotency.complete_idempotency(operation, key, request_hash, response)
        return snapshot

    async def post(
        self,
        context: TrustedTenantContext,
        *,
        candidate_id: str,
        expected_revision: int,
        exchange_rate: ExchangeRateSnapshot | None,
        idempotency_key: str,
    ) -> AccountingCandidate:
        key = normalize_idempotency_key(idempotency_key)
        payload = {
            "candidate_id": candidate_id,
            "expected_revision": expected_revision,
            "exchange_rate_snapshot_id": exchange_rate.snapshot_id if exchange_rate else None,
        }
        operation, request_hash = self._binding(context, "post", payload)
        claim_resource_id = uuid4().hex
        claim = await self._idempotency.claim_idempotency(
            operation, key, request_hash, claim_resource_id
        )
        if claim.status == IdempotencyStatus.COMPLETED:
            assert claim.response_payload is not None
            if claim.response_payload.get("provider_failed") is True:
                raise ServiceUnavailableError("Accounting posting provider failed")
            return _candidate_from_payload(claim.response_payload)
        if claim.resource_id != claim_resource_id:
            raise IdempotencyInProgressError("Accounting posting request is already in progress")
        package = await self._repository.get_approved_package(candidate_id, context.tenant_id)
        if package is None:
            raise ResourceNotFoundError("Approved accounting package was not found")
        candidate, proposal, tax = package
        if candidate.revision != expected_revision:
            from invoice_intelligence.application.errors import ResourceConflictError

            raise ResourceConflictError("Accounting candidate revision is stale")
        started = await self._repository.record_posting_attempt(
            candidate_id=candidate_id,
            tenant_id=context.tenant_id,
            expected_revision=expected_revision,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
            outcome="started",
            external_reference=None,
            error_code=None,
        )
        try:
            receipt = await self._posting.post(
                posting_request_id=claim.resource_id,
                candidate=candidate,
                proposal=proposal,
                tax_assessment=tax,
                exchange_rate=exchange_rate,
            )
        except Exception:
            failed = await self._repository.record_posting_attempt(
                candidate_id=candidate_id,
                tenant_id=context.tenant_id,
                expected_revision=started.revision,
                actor_id=context.actor_id,
                trace_id=context.trace_id,
                outcome="failed",
                external_reference=None,
                error_code="accounting.provider_failed",
            )
            response = _candidate_payload(failed) | {"provider_failed": True}
            await self._idempotency.complete_idempotency(operation, key, request_hash, response)
            raise ServiceUnavailableError("Accounting posting provider failed")
        posted = await self._repository.record_posting_attempt(
            candidate_id=candidate_id,
            tenant_id=context.tenant_id,
            expected_revision=started.revision,
            actor_id=context.actor_id,
            trace_id=context.trace_id,
            outcome="posted",
            external_reference=receipt.external_reference,
            error_code=None,
        )
        await self._idempotency.complete_idempotency(
            operation, key, request_hash, _candidate_payload(posted)
        )
        return posted

    async def list_audit(
        self, context: TrustedTenantContext, candidate_id: str
    ) -> tuple[AccountingAuditEvent, ...]:
        return await self._repository.list_audit(candidate_id, context.tenant_id)

    async def _candidate_write(
        self,
        context: TrustedTenantContext,
        action_name: str,
        idempotency_key: str,
        payload: dict[str, Any],
        resource_id: str,
        action: Callable[[], Awaitable[AccountingCandidate]],
    ) -> AccountingCandidate:
        key = normalize_idempotency_key(idempotency_key)
        operation, request_hash = self._binding(context, action_name, payload)
        claim_resource_id = uuid4().hex
        claim = await self._idempotency.claim_idempotency(
            operation, key, request_hash, claim_resource_id
        )
        if claim.status == IdempotencyStatus.COMPLETED:
            assert claim.response_payload is not None
            return _candidate_from_payload(claim.response_payload)
        if claim.resource_id != claim_resource_id:
            raise IdempotencyInProgressError("Accounting request is already in progress")
        result = await action()
        await self._idempotency.complete_idempotency(
            operation, key, request_hash, _candidate_payload(result)
        )
        return result

    @staticmethod
    def _binding(
        context: TrustedTenantContext, action: str, payload: dict[str, Any]
    ) -> tuple[str, str]:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return (
            f"accounting.{action}.{context.tenant_id}",
            sha256(encoded.encode("utf-8")).hexdigest(),
        )


def _candidate_payload(value: AccountingCandidate) -> dict[str, Any]:
    return {
        "candidate_id": value.candidate_id,
        "tenant_id": value.tenant_id,
        "run_id": value.run_id,
        "status": value.status.value,
        "tax_rule_version": value.tax_rule_version,
        "chart_of_accounts_version": value.chart_of_accounts_version,
        "posting_rule_version": value.posting_rule_version,
        "revision": value.revision,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }


def _candidate_from_payload(payload: dict[str, Any]) -> AccountingCandidate:
    from invoice_intelligence.domain.accounting import AccountingStatus

    return AccountingCandidate(
        candidate_id=str(payload["candidate_id"]),
        tenant_id=str(payload["tenant_id"]),
        run_id=str(payload["run_id"]),
        status=AccountingStatus(str(payload["status"])),
        tax_rule_version=payload.get("tax_rule_version"),
        chart_of_accounts_version=payload.get("chart_of_accounts_version"),
        posting_rule_version=payload.get("posting_rule_version"),
        revision=int(payload["revision"]),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        updated_at=datetime.fromisoformat(str(payload["updated_at"])),
    )


def _snapshot_payload(value: ExchangeRateSnapshot) -> dict[str, Any]:
    return {
        "source": value.source,
        "quote_currency": value.quote_currency,
        "base_currency": value.base_currency,
        "rate": str(value.rate),
        "precision": value.precision,
        "effective_at": value.effective_at.isoformat(),
        "snapshot_id": value.snapshot_id,
    }


def _snapshot_from_payload(payload: dict[str, Any]) -> ExchangeRateSnapshot:
    return ExchangeRateSnapshot(
        source=str(payload["source"]),
        quote_currency=str(payload["quote_currency"]),
        base_currency=str(payload["base_currency"]),
        rate=Decimal(str(payload["rate"])),
        precision=int(payload["precision"]),
        effective_at=datetime.fromisoformat(str(payload["effective_at"])),
        snapshot_id=str(payload["snapshot_id"]),
    )
