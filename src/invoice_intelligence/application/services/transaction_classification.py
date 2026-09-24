"""独立交易分类 Service。"""

from invoice_intelligence.application.ports.transaction_rules import TransactionRuleEngine
from invoice_intelligence.domain.transactions import TransactionCandidate, TransactionClassification


class TransactionClassificationService:
    def __init__(self, rule_engine: TransactionRuleEngine):
        self._rule_engine = rule_engine

    def classify(self, candidate: TransactionCandidate) -> TransactionClassification:
        return self._rule_engine.classify(candidate)
