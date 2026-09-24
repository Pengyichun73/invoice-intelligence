import assert from 'node:assert/strict'
import test from 'node:test'
import {
  buildReviewSubmission,
  focusReviewTarget,
  reviewInputMetadata,
  ReviewSubmissionError,
} from '../src/views/extractionReview.js'

const base = {
  currentInvoice: { voucher_number: 'V-1', is_seal: false },
  reviewFields: [{ field_path: 'voucher_number', current_value: 'V-1' }],
  reviewBindings: [],
  reviewActions: { voucher_number: 'confirm_correct' },
  reviewValues: {},
  reviewReasons: {},
  bindingSelections: {},
  bindingReasons: {},
  reviewNulls: {},
}

test('构造明确确认正确的最小审核请求', () => {
  assert.deepEqual(buildReviewSubmission(base), {
    document_type: null,
    fields: [{ field_path: 'voucher_number', action: 'confirm_correct', reason: null }],
    field_bindings: [],
  })
})

test('人工修正仅更新指定字段并保留完整 InvoiceExtraction', () => {
  const payload = buildReviewSubmission({
    ...base,
    reviewActions: { voucher_number: 'correct' },
    reviewValues: { voucher_number: 'V-2' },
    reviewReasons: { voucher_number: '核对原图' },
  })
  assert.deepEqual(payload.corrected_invoice, { voucher_number: 'V-2', is_seal: false })
  assert.equal(base.currentInvoice.voucher_number, 'V-1')
})

test('字段绑定缺少选择或原因时返回可见校验错误', () => {
  assert.throws(
    () => buildReviewSubmission({
      ...base,
      reviewBindings: [{ evidence_id: 'evidence-1' }],
      bindingSelections: { 'evidence-1': '' },
      bindingReasons: { 'evidence-1': '' },
    }),
    (error) => error instanceof ReviewSubmissionError
      && error.target === 'binding:evidence-1'
      && error.message.includes('字段绑定'),
  )
})

test('空审核任务不得发送到后端', () => {
  assert.throws(
    () => buildReviewSubmission({ ...base, reviewFields: [] }),
    /没有可提交的字段或字段绑定决定/,
  )
})

test('日期与日期时间按固定 Schema 规范化', () => {
  const datePayload = buildReviewSubmission({
    ...base,
    currentInvoice: { ...base.currentInvoice, invoice_date: '2026-09-01' },
    reviewFields: [{ field_path: 'invoice_date', current_value: '2026-09-01' }],
    reviewActions: { invoice_date: 'correct' },
    reviewValues: { invoice_date: '2026-09-23' },
    reviewReasons: { invoice_date: '核对原图' },
  })
  assert.equal(datePayload.corrected_invoice.invoice_date, '2026-09-23')

  const datetimePayload = buildReviewSubmission({
    ...base,
    currentInvoice: { ...base.currentInvoice, bookkeeping_datetime: '2026-09-01T08:00:00' },
    reviewFields: [{ field_path: 'bookkeeping_datetime', current_value: '2026-09-01T08:00:00' }],
    reviewActions: { bookkeeping_datetime: 'correct' },
    reviewValues: { bookkeeping_datetime: '2026-09-23T14:05' },
    reviewReasons: { bookkeeping_datetime: '核对原图' },
  })
  assert.equal(datetimePayload.corrected_invoice.bookkeeping_datetime, '2026-09-23T14:05:00')
})

test('日期时间兼容严格日期并补午夜', () => {
  const payload = buildReviewSubmission({
    ...base,
    currentInvoice: { ...base.currentInvoice, bookkeeping_datetime: '2026-09-01T08:00:00' },
    reviewFields: [{ field_path: 'bookkeeping_datetime', current_value: '2026-09-01T08:00:00' }],
    reviewActions: { bookkeeping_datetime: 'correct' },
    reviewValues: { bookkeeping_datetime: '2026-09-23' },
    reviewReasons: { bookkeeping_datetime: '仅提供入账日期' },
  })
  assert.equal(payload.corrected_invoice.bookkeeping_datetime, '2026-09-23T00:00:00')
})

test('拒绝非法日历日期、非法时间和时区输入', () => {
  for (const value of ['2026-02-29', '2026-02-30', '2026-01-01T24:00', '2026-01-01T08:00Z']) {
    assert.throws(
      () => buildReviewSubmission({
        ...base,
        currentInvoice: { ...base.currentInvoice, bookkeeping_datetime: '2026-09-01T08:00:00' },
        reviewFields: [{ field_path: 'bookkeeping_datetime', current_value: '2026-09-01T08:00:00' }],
        reviewActions: { bookkeeping_datetime: 'correct' },
        reviewValues: { bookkeeping_datetime: value },
        reviewReasons: { bookkeeping_datetime: '核对原图' },
      }),
      (error) => error instanceof ReviewSubmissionError
        && error.target === 'field:bookkeeping_datetime',
    )
  }
})

test('接受闰年日期并拒绝开票日期混入时间', () => {
  const leap = buildReviewSubmission({
    ...base,
    currentInvoice: { ...base.currentInvoice, invoice_date: '2028-02-01' },
    reviewFields: [{ field_path: 'invoice_date', current_value: '2028-02-01' }],
    reviewActions: { invoice_date: 'correct' },
    reviewValues: { invoice_date: '2028-02-29' },
    reviewReasons: { invoice_date: '核对原图' },
  })
  assert.equal(leap.corrected_invoice.invoice_date, '2028-02-29')
  assert.throws(() => buildReviewSubmission({
    ...base,
    currentInvoice: { ...base.currentInvoice, invoice_date: '2028-02-01' },
    reviewFields: [{ field_path: 'invoice_date', current_value: '2028-02-01' }],
    reviewActions: { invoice_date: 'correct' },
    reviewValues: { invoice_date: '2028-02-29T08:00' },
    reviewReasons: { invoice_date: '核对原图' },
  }), /请输入有效日期/)
})

test('日期字段可显式修正为 null', () => {
  const payload = buildReviewSubmission({
    ...base,
    currentInvoice: { ...base.currentInvoice, bookkeeping_datetime: '2026-09-01T08:00:00' },
    reviewFields: [{ field_path: 'bookkeeping_datetime', current_value: '2026-09-01T08:00:00' }],
    reviewActions: { bookkeeping_datetime: 'correct' },
    reviewValues: { bookkeeping_datetime: '' },
    reviewReasons: { bookkeeping_datetime: '原图无此字段' },
    reviewNulls: { bookkeeping_datetime: true },
  })
  assert.equal(payload.corrected_invoice.bookkeeping_datetime, null)
})

test('固定字段返回对应原生输入类型', () => {
  assert.deepEqual(reviewInputMetadata('bookkeeping_datetime'), {
    type: 'datetime-local',
    hint: '请选择日期和时间',
  })
  assert.equal(reviewInputMetadata('invoice_date').type, 'date')
  assert.equal(reviewInputMetadata('voucher_number').type, 'text')
})

test('首个错误目标会滚动并聚焦输入控件', () => {
  let scrolled = false
  let focused = false
  const element = {
    scrollIntoView(options) { scrolled = options.block === 'center' },
    querySelector() { return { focus() { focused = true } } },
  }
  assert.equal(focusReviewTarget('field:bookkeeping_datetime', () => element), true)
  assert.equal(scrolled, true)
  assert.equal(focused, true)
})
