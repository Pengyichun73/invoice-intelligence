export function canShowBenefitMetrics(status, run, scenarios) {
  return status !== 'insufficient_evidence'
    && run?.status === 'completed'
    && run.metrics?.coverage_sufficient === true
    && scenarios?.run_id === run.run_id
}
