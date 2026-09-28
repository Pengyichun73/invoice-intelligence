import assert from 'node:assert/strict'
import test from 'node:test'
import { governanceApi } from '../src/api/governance.js'

test('准入查询将 Run ID、状态和游标交给服务端筛选', async (context) => {
  const originalFetch = globalThis.fetch
  context.after(() => { globalThis.fetch = originalFetch })
  let requestedPath = ''
  globalThis.fetch = async (path) => {
    requestedPath = path
    return {
      ok: true,
      status: 200,
      headers: new Headers(),
      async json() { return { items: [], next_cursor: null } },
    }
  }

  await governanceApi.admissions({ run_id: 'run-1', status: 'quarantined', limit: 20, cursor: 'example-1' })

  const url = new URL(requestedPath, 'http://localhost')
  assert.equal(url.pathname, '/api/v1/memory/admissions')
  assert.equal(url.searchParams.get('run_id'), 'run-1')
  assert.equal(url.searchParams.get('status'), 'quarantined')
  assert.equal(url.searchParams.get('cursor'), 'example-1')
})
