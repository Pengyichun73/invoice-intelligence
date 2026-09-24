"""可替换的确定性开发规则实现。"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from invoice_intelligence.domain.transactions import (
    DuplicateTransactionCase,
    RiskAssessment,
    RiskStatus,
    SuspiciousTransactionCase,
    TransactionCandidate,
    TransactionClassification,
)


class MockTransactionRuleEngine:
    """开发样例：无匹配规则时明确返回 pending_rule_review。"""

    classification_rule_version = "transaction-classification-dev-1"
    risk_rule_version: str | None = None
    model_version = "advisory-none"

    def classify(self, candidate: TransactionCandidate) -> TransactionClassification:
        now = datetime.now(UTC)
        category = "uncategorized"
        if candidate.amount is not None and candidate.amount < 0:
            category = "credit"
        return TransactionClassification(
            classification_id=uuid4().hex,
            tenant_id=candidate.tenant_id,
            candidate_id=candidate.candidate_id,
            category=category,
            subcategory=None,
            rule_version=self.classification_rule_version,
            model_version=self.model_version,
            evidence=({"type": "development_rule", "field": "amount"},),
            idempotency_key_hash="pending",
            created_at=now,
        )

    def assess_risk(
        self, candidate: TransactionCandidate, duplicate: DuplicateTransactionCase | None
    ):
        now = datetime.now(UTC)
        if self.risk_rule_version is None:
            status = RiskStatus.PENDING_RULE_REVIEW
            suspicious = None
        else:
            status = RiskStatus.PENDING
            suspicious = SuspiciousTransactionCase(
                case_id=uuid4().hex,
                tenant_id=candidate.tenant_id,
                candidate_transaction_id=candidate.candidate_id,
                risk_rule="development.no-op",
                rule_version=self.risk_rule_version,
                triggering_evidence=(),
                advisory_score=None,
                idempotency_key_hash="pending",
                created_at=now,
            )
        assessment = RiskAssessment(
            assessment_id=uuid4().hex,
            tenant_id=candidate.tenant_id,
            candidate_transaction_id=candidate.candidate_id,
            status=status,
            rule_version=self.risk_rule_version,
            advisory_score=Decimal("0"),
            evidence=({"type": "rule_evaluation", "status": status.value},),
            idempotency_key_hash="pending",
            created_at=now,
        )
        return duplicate, suspicious, assessment
