import assert from 'node:assert/strict'
import test from 'node:test'
import { apiRequest, write } from '../src/api/client.js'

function response(status, payload, traceId = null) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(traceId ? { 'x-trace-id': traceId } : {}),
    async json() { return payload },
  }
}

test('未知后端错误只暴露白名单文案与技术关联标识', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = async () => response(500, { message: '敏感发票值与远程响应体' }, 'trace-safe')

  await assert.rejects(
    () => apiRequest('/api/v1/private'),
    (error) => error.message === '服务内部错误，请使用 trace 信息排查'
      && error.traceId === 'trace-safe'
      && error.payload === null
      && !error.message.includes('敏感发票值'),
  )
})

test('并发请求在成功和失败后都将活动计数恢复为零', async (context) => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const originalCustomEvent = globalThis.CustomEvent
  context.after(() => {
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
    globalThis.CustomEvent = originalCustomEvent
  })
  class TestCustomEvent extends Event { constructor(type, options) { super(type); this.detail = options.detail } }
  globalThis.CustomEvent = TestCustomEvent
  globalThis.window = new EventTarget()
  const activity = []
  window.addEventListener('invoice:request-activity', (event) => activity.push(event.detail.active))
  const releases = []
  globalThis.fetch = () => new Promise((resolve) => { releases.push(resolve) })

  const first = apiRequest('/one')
  const second = apiRequest('/two')
  releases.forEach((release) => release(response(200, {})))
  await Promise.all([first, second])

  assert.deepEqual(activity, [1, 2, 1, 0])
})

test('结果不确定的相同写请求复用 Idempotency-Key，成功后轮换', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  const keys = []
  let failures = 2
  globalThis.fetch = async (_path, options) => {
    keys.push(options.headers['Idempotency-Key'])
    if (failures > 0) {
      failures -= 1
      throw new TypeError('network down')
    }
    return response(200, { ok: true })
  }

  await assert.rejects(() => write('/api/v1/action', { body: { reason: '同一语义' } }))
  await assert.rejects(() => write('/api/v1/action', { body: { reason: '同一语义' } }))
  await write('/api/v1/action', { body: { reason: '同一语义' } })
  await write('/api/v1/action', { body: { reason: '同一语义' } })

  assert.equal(keys[0], keys[1])
  assert.equal(keys[1], keys[2])
  assert.notEqual(keys[2], keys[3])
})

test('普通请求超过 5 秒后中止并收敛活动计数', async (context) => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const originalCustomEvent = globalThis.CustomEvent
  context.after(() => {
    globalThis.fetch = originalFetch
    globalThis.window = originalWindow
    globalThis.CustomEvent = originalCustomEvent
  })
  class TestCustomEvent extends Event { constructor(type, options) { super(type); this.detail = options.detail } }
  globalThis.CustomEvent = TestCustomEvent
  globalThis.window = new EventTarget()
  const activity = []
  window.addEventListener('invoice:request-activity', (event) => activity.push(event.detail.active))
  globalThis.fetch = (_path, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')), { once: true })
  })

  await assert.rejects(
    () => apiRequest('/slow', { timeoutMs: 10 }),
    (error) => error.code === 'request_timeout' && error.message === '请求超时，请稍后重试',
  )
  assert.deepEqual(activity, [1, 0])
})

test('写请求可单独使用发票提取的 30 秒超时配置', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let receivedTimeoutSignal = false
  globalThis.fetch = (_path, options) => {
    receivedTimeoutSignal = options.signal instanceof AbortSignal
    return Promise.resolve(response(200, { ok: true }))
  }

  await write('/api/v1/documents/document-1/extract', { timeoutMs: 30000 })
  assert.equal(receivedTimeoutSignal, true)
})
