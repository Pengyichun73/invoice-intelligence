export class ReviewSubmissionError extends Error {
  constructor(message, target = null) {
    super(message)
    this.name = 'ReviewSubmissionError'
    this.target = target
  }
}

const REVIEW_INPUTS = Object.freeze({
  bookkeeping_datetime: Object.freeze({ type: 'datetime-local', hint: '请选择日期和时间' }),
  invoice_date: Object.freeze({ type: 'date', hint: '请选择日期' }),
})

export function reviewInputMetadata(fieldPath) {
  return REVIEW_INPUTS[fieldPath] || { type: 'text', hint: '请输入修正值' }
}

function parseReviewValue(value) {
  try { return JSON.parse(String(value)) } catch { return String(value) }
}

function isValidDateParts(year, month, day) {
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= monthDays[month - 1]
}

function normalizeDate(value, fieldPath) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match || !isValidDateParts(Number(match[1]), Number(match[2]), Number(match[3]))) {
    throw new ReviewSubmissionError('请输入有效日期，格式为 YYYY-MM-DD', `field:${fieldPath}`)
  }
  return value
}

function normalizeDateTime(value, fieldPath) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${normalizeDate(value, fieldPath)}T00:00:00`
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value)
  if (
    !match
    || !isValidDateParts(Number(match[1]), Number(match[2]), Number(match[3]))
    || Number(match[4]) > 23
    || Number(match[5]) > 59
    || Number(match[6] || 0) > 59
  ) {
    throw new ReviewSubmissionError('请输入有效日期时间，格式为 YYYY-MM-DD HH:mm', `field:${fieldPath}`)
  }
  return `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6] || '00'}`
}

function normalizeReviewValue(fieldPath, value) {
  const text = String(value).trim()
  if (fieldPath === 'bookkeeping_datetime') return normalizeDateTime(text, fieldPath)
  if (fieldPath === 'invoice_date') return normalizeDate(text, fieldPath)
  return parseReviewValue(value)
}

export function focusReviewTarget(target, resolveElement) {
  if (!target || typeof resolveElement !== 'function') return false
  const element = resolveElement(target)
  if (!element) return false
  element.scrollIntoView?.({ behavior: 'smooth', block: 'center' })
  element.querySelector?.('[aria-invalid="true"], [data-review-control]:not([disabled]), select:not([disabled]), input:not([disabled])')?.focus?.()
  return true
}

function setPath(target, fieldPath, value) {
  const parts = fieldPath.split('.')
  let current = target
  parts.slice(0, -1).forEach((part) => { current = current[part] ||= {} })
  current[parts.at(-1)] = value
}

export function buildReviewSubmission({
  currentInvoice,
  reviewFields,
  reviewBindings,
  reviewActions,
  reviewValues,
  reviewReasons,
  bindingSelections,
  bindingReasons,
  reviewNulls = {},
}) {
  if (!currentInvoice || typeof currentInvoice !== 'object') {
    throw new ReviewSubmissionError('审核上下文没有可修正的 InvoiceExtraction')
  }

  const incompleteBinding = reviewBindings.find(
    (item) => !bindingSelections[item.evidence_id] || !bindingReasons[item.evidence_id]?.trim(),
  )
  if (incompleteBinding) {
    throw new ReviewSubmissionError(
      '每条字段绑定都必须选择标准字段路径并填写原因',
      `binding:${incompleteBinding.evidence_id}`,
    )
  }

  const fields = reviewFields.map((field) => {
    const fieldPath = field.field_path
    const action = reviewActions[fieldPath]
    const reason = reviewReasons[fieldPath]?.trim() || null
    if (['correct', 'confirm_incorrect'].includes(action) && !reason) {
      throw new ReviewSubmissionError('修正或否定字段必须填写原因', `field:${fieldPath}`)
    }
    if (action === 'correct' && !reviewNulls[fieldPath] && !String(reviewValues[fieldPath] ?? '').trim()) {
      throw new ReviewSubmissionError('选择人工修正后必须填写新的字段值', `field:${fieldPath}`)
    }
    return {
      field_path: fieldPath,
      action,
      reason,
      ...(action === 'confirm_incorrect' ? { rejected_value: field.current_value } : {}),
    }
  })

  if (!fields.length && !reviewBindings.length) {
    throw new ReviewSubmissionError('当前审核任务没有可提交的字段或字段绑定决定')
  }

  const payload = {
    document_type: null,
    fields,
    field_bindings: reviewBindings.map((item) => ({
      evidence_id: item.evidence_id,
      selected_canonical_field_path: bindingSelections[item.evidence_id],
      reason: bindingReasons[item.evidence_id].trim(),
    })),
  }
  if (fields.some((item) => item.action === 'correct')) {
    const correctedInvoice = JSON.parse(JSON.stringify(currentInvoice))
    fields.filter((item) => item.action === 'correct').forEach((item) => {
      setPath(
        correctedInvoice,
        item.field_path,
        reviewNulls[item.field_path]
          ? null
          : normalizeReviewValue(item.field_path, reviewValues[item.field_path]),
      )
    })
    payload.corrected_invoice = correctedInvoice
  }
  return payload
}

export function buildReviewSubmissionEnvelope({ expectedRevision, leaseToken, correction }) {
  if (!Number.isInteger(expectedRevision) || expectedRevision < 1) {
    throw new ReviewSubmissionError('审核任务版本无效')
  }
  if (typeof leaseToken !== 'string' || leaseToken.length !== 64) {
    throw new ReviewSubmissionError('审核租约无效，请重新领取审核任务')
  }
  if (!correction || typeof correction !== 'object') {
    throw new ReviewSubmissionError('审核决定不能为空')
  }
  return {
    expected_revision: expectedRevision,
    lease_token: leaseToken,
    correction,
  }
}
