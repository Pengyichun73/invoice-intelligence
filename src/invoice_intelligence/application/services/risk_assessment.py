"""风险状态确定性 Policy 入口。"""

from invoice_intelligence.domain.transactions import RiskStatus, TransactionReviewStatus


def final_risk_status(
    *, reviewer_decision: TransactionReviewStatus | None, has_rules: bool
) -> RiskStatus:
    if reviewer_decision is not None:
        return RiskStatus(reviewer_decision.value)
    return RiskStatus.PENDING if has_rules else RiskStatus.PENDING_RULE_REVIEW
