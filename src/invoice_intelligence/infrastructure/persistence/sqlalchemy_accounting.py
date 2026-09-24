"""SQLAlchemy persistence for tenant-scoped accounting facts."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from invoice_intelligence.application.errors import ResourceConflictError, ResourceNotFoundError
from invoice_intelligence.domain.accounting import (
    AccountingAdvisory,
    AccountingAuditEvent,
    AccountingCandidate,
    AccountingStatus,
    ExchangeRateSnapshot,
    PostingLine,
    PostingProposal,
    TaxAssessment,
)
from invoice_intelligence.infrastructure.persistence.sqlalchemy_models import (
    AccountingAuditRow,
    AccountingCandidateRow,
    AccountingPostingAttemptRow,
    ExchangeRateSnapshotRow,
    ExtractionResultRow,
    ExtractionRunRow,
    PostingProposalRow,
    TaxAssessmentRow,
)


class SQLAlchemyAccountingRepository:
    def __init__(self, engine: Engine) -> None:
        self._sessions = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)

    async def create_candidate(
        self, *, tenant_id: str, run_id: str, actor_id: str, trace_id: str | None
    ) -> AccountingCandidate:
        return await asyncio.to_thread(
            self._create_candidate,
            tenant_id=tenant_id,
            run_id=run_id,
            actor_id=actor_id,
            trace_id=trace_id,
        )

    def _create_candidate(
        self, *, tenant_id: str, run_id: str, actor_id: str, trace_id: str | None
    ) -> AccountingCandidate:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            completed = session.scalar(
                select(ExtractionRunRow)
                .join(ExtractionResultRow, ExtractionResultRow.run_id == ExtractionRunRow.run_id)
                .where(
                    ExtractionRunRow.run_id == run_id,
                    ExtractionRunRow.tenant_id == tenant_id,
                    ExtractionRunRow.status == "completed",
                )
            )
            if completed is None:
                raise ResourceNotFoundError("Completed extraction run was not found")
            existing = session.scalar(
                select(AccountingCandidateRow).where(
                    AccountingCandidateRow.tenant_id == tenant_id,
                    AccountingCandidateRow.run_id == run_id,
                )
            )
            if existing is not None:
                return _candidate(existing)
            row = AccountingCandidateRow(
                candidate_id=uuid4().hex,
                tenant_id=tenant_id,
                run_id=run_id,
                status=AccountingStatus.PENDING_RULE_REVIEW.value,
                revision=1,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.add(
                AccountingAuditRow(
                    audit_id=uuid4().hex,
                    tenant_id=tenant_id,
                    candidate_id=row.candidate_id,
                    action="create_candidate",
                    actor_id=actor_id,
                    from_status=None,
                    to_status=row.status,
                    revision=1,
                    reason_code="accounting.rules_required",
                    trace_id=trace_id,
                    created_at=now,
                )
            )
            return _candidate(row)

    async def get_candidate(self, candidate_id: str, tenant_id: str) -> AccountingCandidate | None:
        return await asyncio.to_thread(self._get_candidate, candidate_id, tenant_id)

    def _get_candidate(self, candidate_id: str, tenant_id: str) -> AccountingCandidate | None:
        with self._sessions() as session:
            row = session.scalar(
                select(AccountingCandidateRow).where(
                    AccountingCandidateRow.candidate_id == candidate_id,
                    AccountingCandidateRow.tenant_id == tenant_id,
                )
            )
            return _candidate(row) if row is not None else None

    async def approve(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        tax_rule_version: str,
        chart_of_accounts_version: str,
        posting_rule_version: str,
        tax_code: str,
        taxable_amount: Decimal,
        tax_amount: Decimal,
        advisory: AccountingAdvisory | None,
        lines: tuple[PostingLine, ...],
    ) -> AccountingCandidate:
        return await asyncio.to_thread(
            self._approve,
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            trace_id=trace_id,
            tax_rule_version=tax_rule_version,
            chart_of_accounts_version=chart_of_accounts_version,
            posting_rule_version=posting_rule_version,
            tax_code=tax_code,
            taxable_amount=taxable_amount,
            tax_amount=tax_amount,
            advisory=advisory,
            lines=lines,
        )

    def _approve(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        tax_rule_version: str,
        chart_of_accounts_version: str,
        posting_rule_version: str,
        tax_code: str,
        taxable_amount: Decimal,
        tax_amount: Decimal,
        advisory: AccountingAdvisory | None,
        lines: tuple[PostingLine, ...],
    ) -> AccountingCandidate:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._required(session, candidate_id, tenant_id)
            if row.status != AccountingStatus.PENDING_RULE_REVIEW.value:
                raise ResourceConflictError("Only pending candidates can be approved")
            self._cas(row, expected_revision)
            row.status = AccountingStatus.APPROVED.value
            row.tax_rule_version = tax_rule_version
            row.chart_of_accounts_version = chart_of_accounts_version
            row.posting_rule_version = posting_rule_version
            row.revision += 1
            row.updated_at = now
            session.add(
                TaxAssessmentRow(
                    assessment_id=uuid4().hex,
                    candidate_id=candidate_id,
                    tax_rule_version=tax_rule_version,
                    tax_code=tax_code,
                    taxable_amount=taxable_amount,
                    tax_amount=tax_amount,
                    advisory_json=(
                        {
                            "provider": advisory.provider,
                            "model_version": advisory.model_version,
                            "recommendation_code": advisory.recommendation_code,
                            "reason_codes": list(advisory.reason_codes),
                        }
                        if advisory is not None
                        else None
                    ),
                    created_at=now,
                )
            )
            session.add(
                PostingProposalRow(
                    proposal_id=uuid4().hex,
                    candidate_id=candidate_id,
                    chart_of_accounts_version=chart_of_accounts_version,
                    posting_rule_version=posting_rule_version,
                    lines_json=[
                        {
                            "account_code": line.account_code,
                            "side": line.side,
                            "amount": str(line.amount),
                            "currency": line.currency,
                        }
                        for line in lines
                    ],
                    decided_by=actor_id,
                    created_at=now,
                )
            )
            self._audit(
                session,
                row,
                actor_id,
                trace_id,
                "approve",
                "accounting.reviewer_approved",
                AccountingStatus.PENDING_RULE_REVIEW,
            )
            return _candidate(row)

    async def reject(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        reason_code: str,
    ) -> AccountingCandidate:
        return await asyncio.to_thread(
            self._reject,
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            trace_id=trace_id,
            reason_code=reason_code,
        )

    def _reject(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        reason_code: str,
    ) -> AccountingCandidate:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._required(session, candidate_id, tenant_id)
            if row.status != AccountingStatus.PENDING_RULE_REVIEW.value:
                raise ResourceConflictError("Only pending candidates can be rejected")
            self._cas(row, expected_revision)
            row.status = AccountingStatus.REJECTED.value
            row.revision += 1
            row.updated_at = now
            self._audit(
                session,
                row,
                actor_id,
                trace_id,
                "reject",
                reason_code,
                AccountingStatus.PENDING_RULE_REVIEW,
            )
            return _candidate(row)

    async def get_approved_package(
        self, candidate_id: str, tenant_id: str
    ) -> tuple[AccountingCandidate, PostingProposal, TaxAssessment] | None:
        return await asyncio.to_thread(self._get_approved_package, candidate_id, tenant_id)

    def _get_approved_package(
        self, candidate_id: str, tenant_id: str
    ) -> tuple[AccountingCandidate, PostingProposal, TaxAssessment] | None:
        with self._sessions() as session:
            row = self._required(session, candidate_id, tenant_id)
            if row.status != AccountingStatus.APPROVED.value:
                raise ResourceConflictError("Only approved candidates can be posted")
            proposal = session.scalar(
                select(PostingProposalRow).where(PostingProposalRow.candidate_id == candidate_id)
            )
            tax = session.scalar(
                select(TaxAssessmentRow).where(TaxAssessmentRow.candidate_id == candidate_id)
            )
            if proposal is None or tax is None:
                raise ResourceConflictError("Approved accounting package is incomplete")
            lines = tuple(
                PostingLine(
                    account_code=item["account_code"],
                    side=item["side"],
                    amount=Decimal(item["amount"]),
                    currency=item["currency"],
                )
                for item in proposal.lines_json
            )
            return (
                _candidate(row),
                PostingProposal(
                    proposal_id=proposal.proposal_id,
                    candidate_id=candidate_id,
                    chart_of_accounts_version=proposal.chart_of_accounts_version,
                    posting_rule_version=proposal.posting_rule_version,
                    lines=lines,
                    decided_by=proposal.decided_by,
                    created_at=proposal.created_at,
                ),
                TaxAssessment(
                    assessment_id=tax.assessment_id,
                    candidate_id=candidate_id,
                    tax_rule_version=tax.tax_rule_version,
                    tax_code=tax.tax_code,
                    taxable_amount=Decimal(tax.taxable_amount),
                    tax_amount=Decimal(tax.tax_amount),
                    advisory=(
                        AccountingAdvisory(
                            provider=str(tax.advisory_json["provider"]),
                            model_version=str(tax.advisory_json["model_version"]),
                            recommendation_code=str(tax.advisory_json["recommendation_code"]),
                            reason_codes=tuple(
                                str(value) for value in tax.advisory_json["reason_codes"]
                            ),
                        )
                        if tax.advisory_json is not None
                        else None
                    ),
                    created_at=tax.created_at,
                ),
            )

    async def save_exchange_rate(
        self, tenant_id: str, snapshot: ExchangeRateSnapshot
    ) -> ExchangeRateSnapshot:
        return await asyncio.to_thread(self._save_exchange_rate, tenant_id, snapshot)

    def _save_exchange_rate(
        self, tenant_id: str, snapshot: ExchangeRateSnapshot
    ) -> ExchangeRateSnapshot:
        with self._sessions.begin() as session:
            existing = session.get(ExchangeRateSnapshotRow, snapshot.snapshot_id)
            if existing is None:
                session.add(
                    ExchangeRateSnapshotRow(
                        snapshot_id=snapshot.snapshot_id,
                        tenant_id=tenant_id,
                        source=snapshot.source,
                        quote_currency=snapshot.quote_currency,
                        base_currency=snapshot.base_currency,
                        rate=snapshot.rate,
                        precision=snapshot.precision,
                        effective_at=snapshot.effective_at,
                        created_at=datetime.now(UTC),
                    )
                )
            elif existing.tenant_id != tenant_id:
                raise ResourceNotFoundError("Exchange-rate snapshot was not found")
        return snapshot

    async def record_posting_attempt(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        outcome: str,
        external_reference: str | None,
        error_code: str | None,
    ) -> AccountingCandidate:
        return await asyncio.to_thread(
            self._record_posting_attempt,
            candidate_id=candidate_id,
            tenant_id=tenant_id,
            expected_revision=expected_revision,
            actor_id=actor_id,
            trace_id=trace_id,
            outcome=outcome,
            external_reference=external_reference,
            error_code=error_code,
        )

    def _record_posting_attempt(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        expected_revision: int,
        actor_id: str,
        trace_id: str | None,
        outcome: str,
        external_reference: str | None,
        error_code: str | None,
    ) -> AccountingCandidate:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = self._required(session, candidate_id, tenant_id)
            if row.status != AccountingStatus.APPROVED.value:
                raise ResourceConflictError("Only approved candidates can be posted")
            self._cas(row, expected_revision)
            previous = AccountingStatus.APPROVED
            row.revision += 1
            if outcome == "posted":
                row.status = AccountingStatus.POSTED.value
            row.updated_at = now
            session.add(
                AccountingPostingAttemptRow(
                    attempt_id=uuid4().hex,
                    tenant_id=tenant_id,
                    candidate_id=candidate_id,
                    outcome=outcome,
                    external_reference=external_reference,
                    error_code=error_code,
                    created_at=now,
                )
            )
            reason_code = (
                "accounting.posted"
                if outcome == "posted"
                else "accounting.posting_started"
                if outcome == "started"
                else (error_code or "accounting.provider_failed")
            )
            self._audit(
                session,
                row,
                actor_id,
                trace_id,
                "post" if outcome == "posted" else outcome,
                reason_code,
                previous,
            )
            return _candidate(row)

    async def list_audit(
        self, candidate_id: str, tenant_id: str
    ) -> tuple[AccountingAuditEvent, ...]:
        return await asyncio.to_thread(self._list_audit, candidate_id, tenant_id)

    def _list_audit(self, candidate_id: str, tenant_id: str) -> tuple[AccountingAuditEvent, ...]:
        with self._sessions() as session:
            self._required(session, candidate_id, tenant_id)
            rows = session.scalars(
                select(AccountingAuditRow)
                .where(
                    AccountingAuditRow.candidate_id == candidate_id,
                    AccountingAuditRow.tenant_id == tenant_id,
                )
                .order_by(AccountingAuditRow.revision)
            ).all()
            return tuple(
                AccountingAuditEvent(
                    audit_id=row.audit_id,
                    tenant_id=row.tenant_id,
                    candidate_id=row.candidate_id,
                    action=row.action,
                    actor_id=row.actor_id,
                    from_status=AccountingStatus(row.from_status) if row.from_status else None,
                    to_status=AccountingStatus(row.to_status),
                    revision=row.revision,
                    reason_code=row.reason_code,
                    trace_id=row.trace_id,
                    created_at=row.created_at,
                )
                for row in rows
            )

    @staticmethod
    def _required(session: Session, candidate_id: str, tenant_id: str) -> AccountingCandidateRow:
        row = session.scalar(
            select(AccountingCandidateRow)
            .where(
                AccountingCandidateRow.candidate_id == candidate_id,
                AccountingCandidateRow.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        if row is None:
            raise ResourceNotFoundError("Accounting candidate was not found")
        return row

    @staticmethod
    def _cas(row: AccountingCandidateRow, expected_revision: int) -> None:
        if row.revision != expected_revision:
            raise ResourceConflictError("Accounting candidate revision is stale")

    @staticmethod
    def _audit(
        session: Session,
        row: AccountingCandidateRow,
        actor_id: str,
        trace_id: str | None,
        action: str,
        reason_code: str,
        previous: AccountingStatus,
    ) -> None:
        session.add(
            AccountingAuditRow(
                audit_id=uuid4().hex,
                tenant_id=row.tenant_id,
                candidate_id=row.candidate_id,
                action=action,
                actor_id=actor_id,
                from_status=previous.value,
                to_status=row.status,
                revision=row.revision,
                reason_code=reason_code,
                trace_id=trace_id,
                created_at=row.updated_at,
            )
        )


def _candidate(row: AccountingCandidateRow) -> AccountingCandidate:
    return AccountingCandidate(
        candidate_id=row.candidate_id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        status=AccountingStatus(row.status),
        tax_rule_version=row.tax_rule_version,
        chart_of_accounts_version=row.chart_of_accounts_version,
        posting_rule_version=row.posting_rule_version,
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
