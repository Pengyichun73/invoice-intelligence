import assert from 'node:assert/strict'
import test from 'node:test'
import { governanceApi } from '../src/api/governance.js'
import { canShowBenefitMetrics } from '../src/views/effectivenessPresentation.js'

test('证据不足或批次错配时不展示收益指标', () => {
  const run = { run_id: 'benefit-1', status: 'completed', metrics: { coverage_sufficient: true } }
  assert.equal(canShowBenefitMetrics('insufficient_evidence', run, { run_id: 'benefit-1' }), false)
  assert.equal(canShowBenefitMetrics('demonstrated', run, { run_id: 'benefit-2' }), false)
  assert.equal(canShowBenefitMetrics('demonstrated', { ...run, metrics: { coverage_sufficient: false } }, { run_id: 'benefit-1' }), false)
  assert.equal(canShowBenefitMetrics('demonstrated', run, { run_id: 'benefit-1' }), true)
})

test('效果查询与旧 Suite 评估使用独立路径和编号', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const paths = []
  globalThis.fetch = async (path) => {
    paths.push(path)
    return { ok: true, status: 200, headers: new Headers(), json: async () => ({}) }
  }
  await governanceApi.effectivenessOverview()
  await governanceApi.effectivenessStages()
  await governanceApi.effectivenessScenarios({ run_id: 'benefit-1', field_path: 'invoice_number' })
  await governanceApi.effectivenessRun('benefit-1')
  await governanceApi.evaluation('suite-1')
  assert.deepEqual(paths, [
    '/api/v1/memory/effectiveness/overview',
    '/api/v1/memory/effectiveness/stages',
    '/api/v1/memory/effectiveness/scenarios?run_id=benefit-1&field_path=invoice_number',
    '/api/v1/memory/effectiveness/runs/benefit-1',
    '/api/v1/memory/evaluations/suite-1',
  ])
})

test('Gold 和 benefit Job 不传租户或审核人身份', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const requests = []
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options })
    return { ok: true, status: 200, headers: new Headers(), json: async () => ({}) }
  }
  await governanceApi.goldCase('doc-1')
  await governanceApi.submitGoldAnnotation('doc-1', { template_group: 'group', versions: {}, fields: {} })
  await governanceApi.createMemoryBenefitJob(['doc-1'])
  assert.deepEqual(requests.map((item) => item.path), [
    '/api/v1/memory/gold/doc-1',
    '/api/v1/memory/gold/doc-1/annotations',
    '/api/v1/evaluations/memory-benefit-jobs',
  ])
  assert.deepEqual(JSON.parse(requests[2].options.body), { document_ids: ['doc-1'] })
  assert.equal(requests.some((item) => /tenant|reviewer/i.test(JSON.stringify(item.options.body))), false)
})
