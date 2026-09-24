import assert from 'node:assert/strict'
import test from 'node:test'
import { apiRequest, requestValidationFields } from '../src/api/client.js'

test('仅解析固定 InvoiceExtraction 字段和已知错误类型', () => {
  const result = requestValidationFields({
    code: 'request_validation_error',
    message: 'Request validation failed: body.corrected_invoice.bookkeeping_datetime:datetime_from_date_parsing; body.corrected_invoice.invoice_date:date_from_datetime_parsing; body.corrected_invoice.tenant_id:string_type',
  })
  assert.deepEqual(result, {
    bookkeeping_datetime: '请输入有效的入账日期和时间',
    invoice_date: '请输入有效的开票日期',
  })
})

test('非请求校验错误和未知诊断不产生字段映射', () => {
  assert.deepEqual(requestValidationFields({ code: 'unprocessable_entity', message: 'x' }), {})
  assert.deepEqual(requestValidationFields({ code: 'request_validation_error', message: 'private response' }), {})
})

test('请求校验错误不向用户展示原始远端诊断', async () => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => new Response(JSON.stringify({
    code: 'request_validation_error',
    message: 'Request validation failed: body.corrected_invoice.bookkeeping_datetime:datetime_from_date_parsing',
  }), { status: 422, headers: { 'Content-Type': 'application/json' } })
  try {
    await assert.rejects(
      () => apiRequest('/api/v1/reviews/run_test'),
      (error) => error.message === '请求字段校验失败'
        && error.fieldErrors.bookkeeping_datetime === '请输入有效的入账日期和时间',
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})
