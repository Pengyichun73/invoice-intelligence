import assert from 'node:assert/strict'
import test from 'node:test'
import { invoiceApi } from '../src/api/invoice.js'

function response(status, payload, traceId = null) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(traceId ? { 'x-trace-id': traceId } : {}),
    async json() { return payload },
  }
}

test('正式审核 API 使用编码路径、正确请求体和嵌套响应', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const requests = []
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options })
    if (requests.length === 1) {
      return response(200, { task: { review_id: 'review/1', revision: 2 }, lease_token: 'a'.repeat(64) })
    }
    return response(200, { task: { review_id: 'review/1', status: 'submitted' }, run: { run_id: 'run-1', status: 'completed' } })
  }

  const claim = await invoiceApi.claimReview('review/1', 1)
  const correction = { document_type: null, fields: [], field_bindings: [] }
  const submitted = await invoiceApi.submitClaimedReview('review/1', {
    expected_revision: 2,
    lease_token: 'a'.repeat(64),
    correction,
  })

  assert.equal(claim.lease_token.length, 64)
  assert.equal(submitted.run.status, 'completed')
  assert.equal(requests[0].path, '/api/v1/reviews/review%2F1/claim')
  assert.deepEqual(JSON.parse(requests[0].options.body), { expected_revision: 1 })
  assert.equal(requests[0].options.headers['Idempotency-Key'].length > 0, true)
  assert.equal(requests[1].path, '/api/v1/reviews/review%2F1/submit')
  assert.deepEqual(JSON.parse(requests[1].options.body), {
    expected_revision: 2,
    lease_token: 'a'.repeat(64),
    correction,
  })
})

test('claim 超时只失败一次，不由 API Client 自动重放', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let calls = 0
  globalThis.fetch = async () => {
    calls += 1
    throw new TypeError('network down')
  }

  await assert.rejects(() => invoiceApi.claimReview('review-1', 1))
  assert.equal(calls, 1)
})

test('submit 409 保留技术关联标识但不暴露远端正文', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = async () => response(409, { message: '敏感业务正文' }, 'trace-review')

  await assert.rejects(
    () => invoiceApi.submitClaimedReview('review-1', {
      expected_revision: 1,
      lease_token: 'a'.repeat(64),
      correction: { document_type: null, fields: [], field_bindings: [] },
    }),
    (error) => error.status === 409
      && error.traceId === 'trace-review'
      && error.payload === null
      && !error.message.includes('敏感业务正文'),
  )
})

test('上传和发票提取请求使用独立的 30 秒超时预算', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const requests = []
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options })
    return response(200, path.endsWith('/extract')
      ? { run_id: 'run-1', status: 'processing' }
      : { document_id: 'document-1' })
  }

  await invoiceApi.upload(new File(['invoice'], 'invoice.png', { type: 'image/png' }))
  await invoiceApi.extract('document-1')
  assert.equal(requests.length, 2)
  assert.equal(requests.every(({ options }) => options.signal instanceof AbortSignal), true)
})
