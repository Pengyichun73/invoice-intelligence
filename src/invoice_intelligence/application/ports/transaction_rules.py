"""交易分析规则与事实持久化 Port。"""

from typing import Protocol

from invoice_intelligence.domain.transactions import (
    DuplicateTransactionCase,
    RiskAssessment,
    SuspiciousTransactionCase,
    TransactionCandidate,
    TransactionClassification,
)


class TransactionRuleEngine(Protocol):
    def classify(self, candidate: TransactionCandidate) -> TransactionClassification: ...

    def assess_risk(
        self,
        candidate: TransactionCandidate,
        duplicate: DuplicateTransactionCase | None,
    ) -> tuple[
        DuplicateTransactionCase | None,
        SuspiciousTransactionCase | None,
        RiskAssessment,
    ]: ...


class TransactionAnalysisRepository(Protocol):
    async def save_analysis(
        self,
        candidate: TransactionCandidate,
        classification: TransactionClassification,
        duplicate: DuplicateTransactionCase | None,
        suspicious: SuspiciousTransactionCase | None,
        assessment: RiskAssessment,
    ) -> RiskAssessment: ...

    async def get_assessment(self, tenant_id: str, candidate_id: str) -> RiskAssessment | None: ...

    async def get_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> TransactionCandidate | None: ...

    async def get_audit_result(self, tenant_id: str, key_hash: str) -> RiskAssessment | None: ...

    async def find_duplicate(
        self, candidate: TransactionCandidate
    ) -> TransactionCandidate | None: ...

    async def list_pending_candidates(self, limit: int) -> tuple[TransactionCandidate, ...]: ...

    async def save_review(
        self, assessment: RiskAssessment, expected_revision: int
    ) -> RiskAssessment: ...
