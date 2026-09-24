const STATUS_MESSAGES = {
  400: '请求参数不符合接口要求',
  403: '无权限执行此操作，或二级审批人与原审核人冲突',
  404: '请求的记录不存在',
  409: '数据已发生变化，请刷新后重试',
  422: '请求字段校验失败',
  429: '请求过于频繁，请稍后重试',
  500: '服务内部错误，请使用 trace 信息排查',
  503: '服务暂时降级，请稍后重试',
}

const RECOVERY_MESSAGES = {
  0: '请确认后端已启动并检查网络连接，然后重试。',
  403: '请确认当前账号具备对应治理权限。',
  409: '页面中的数据已经过期，请读取最新状态后重新操作。',
  429: '系统正在限流，请稍候再提交，避免连续点击。',
  503: '依赖服务暂时不可用，当前数据不会丢失，请稍后重试。',
}

let activeRequests = 0
const retryableWriteKeys = new Map()
const WRITE_KEY_TTL_MS = 5 * 60 * 1000

function publishRequestActivity(delta) {
  activeRequests = Math.max(0, activeRequests + delta)
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('invoice:request-activity', { detail: { active: activeRequests } }))
  }
}

const INVOICE_FIELDS = Object.freeze({
  invoice_unique_code: '发票唯一编号', company_name: '公司名称', invoice_collection_type_desc: '采集方式',
  invoice_number: '发票号', po_number: 'PO号', bookkeeping_datetime: '入账日期', buyer_name: '购方名称',
  attribute_1: '供应商编号', seller_name: '销货方名称', invoice_total_amount: '金额',
  invoice_total_tax_amount: '税额', invoice_total_tax_price: '价税合计', invoice_date: '开票日期',
  currency: '币种', is_seal: 'DN/CN', attribute_2: '其他', invoice_remark: '发票备注',
  batch_code: '批次号', voucher_number: '凭证号',
})

const VALIDATION_TYPE_MESSAGES = Object.freeze({
  datetime_from_date_parsing: (label) => `请输入有效的${label}和时间`,
  datetime_parsing: (label) => `请输入有效的${label}和时间`,
  date_from_datetime_parsing: (label) => `请输入有效的${label}`,
  date_from_datetime_inexact: (label) => `请输入不含时间的${label}`,
  date_parsing: (label) => `请输入有效的${label}`,
  decimal_parsing: (label) => `${label}必须是有效数字`,
  string_type: (label) => `${label}必须是文本`,
  missing: (label) => `缺少${label}字段`,
  extra_forbidden: (label) => `${label}不是允许提交的字段`,
})

export function requestValidationFields(payload) {
  if (payload?.code !== 'request_validation_error' || typeof payload.message !== 'string') return {}
  const fields = {}
  const pattern = /body\.corrected_invoice\.([a-z_][a-z0-9_]*):([a-z_][a-z0-9_]*)(?=;|$)/g
  for (const match of payload.message.matchAll(pattern)) {
    const [, fieldPath, errorType] = match
    const label = INVOICE_FIELDS[fieldPath]
    const formatter = VALIDATION_TYPE_MESSAGES[errorType]
    if (label && formatter && !(fieldPath in fields)) fields[fieldPath] = formatter(label)
  }
  return fields
}

// 后端仍使用稳定英文枚举；这里只负责客户界面的中文呈现，不改变请求参数。
const DISPLAY_LABELS = {
  status: {
    pending: '待准入', approved: '已批准', quarantined: '已隔离', rejected: '已拒绝',
    suspended: '已暂停', invalidated: '已失效', processing: '处理中', completed: '已完成',
    failed: '失败', indexed: '已登记索引', open: '待处理', resolved: '已解决', dismissed: '已忽略',
  },
  operation: {
    approve_admission: '批准记忆准入', reject_admission: '拒绝记忆准入', quarantine_admission: '隔离记忆准入',
    requeue_admission: '重新排队评估', approve_field_alias: '批准字段别名', disable_field_alias: '禁用字段别名',
    resolve_conflict: '解决字段冲突', dismiss_conflict: '忽略字段冲突', disable_example: '禁用案例',
    invalidate_schema: '使 Schema 失效', rebuild_index: '登记索引重建', submit_feedback: '提交检索反馈',
  },
  label_type: { confirmed_correct: '确认正确', corrected: '人工纠正', confirmed_incorrect: '确认错误（负例）' },
  conflict_status: { open: '待处理', resolved: '已解决', dismissed: '已忽略' },
  conflict_type: { field_mapping: '字段映射冲突', semantic_binding: '字段语义冲突', evidence_conflict: '证据冲突', duplicate_alias: '别名重复' },
  alias_status: { pending: '待审批', approved: '已批准', rejected: '已拒绝', suspended: '已暂停', invalidated: '已失效' },
  evaluation_variant: { no_memory: '不使用记忆', pgvector_legacy: '历史向量方案', dense_only: '仅向量检索', sparse_only: '仅关键词检索', hybrid: '混合检索', hybrid_reranker: '混合检索 + 重排', hybrid_positive_negative_few_shot: '混合检索 + 正负案例', no_field_catalog: '无字段目录', static_field_descriptions: '静态字段描述', field_descriptions_aliases: '字段描述 + 别名', hybrid_context_anchors: '混合检索 + 上下文锚点' },
  evaluation_suite: { case_rag: '案例检索评估', trusted_memory_field_binding: '可信记忆与字段绑定评估' },
  bucket_dimension: { overall: '总体', document_type: '单据类型', field_path: '字段', vendor_template: '供应商模板', image_quality: '图片质量' },
  assessment_source: { deterministic: '确定性校验', model_advisory: '模型建议' },
  projection_status: { pending: '待处理', processing: '处理中', indexed: '已登记索引', failed: '投影失败', invalidated: '已失效' },
  evidence_source: { vision: '视觉识别', ocr: '文字识别', paddleocr: '本地文字识别', qwen: '云端视觉识别', combined: '多源结果' },
  provider: { paddlex_http: '本地 PaddleX', paddleocr: '本地 PaddleOCR', qwen: '千问云端', openai: 'OpenAI 云端', vision: '视觉模型' },
  document_type: { invoice: '发票', vat_invoice: '增值税发票', non_vat_invoice: '非增值税发票' },
  value_type: { string: '文本', str: '文本', decimal: '金额', date: '日期', datetime: '日期时间', boolean: '是/否', integer: '整数', number: '数值' },
  resource_type: { memory_admission: '记忆准入', field_alias: '字段别名', memory_conflict: '记忆冲突', reviewed_example: '审核案例', index_version: '索引版本', schema: 'Schema' },
}

export function displayLabel(value, group = 'status') {
  if (value === null || value === undefined || value === '') return '未记录'
  return DISPLAY_LABELS[group]?.[value] || value
}

const TEXT_ENUM_LABELS = {
  confirmed_correct: '明确确认正确',
  confirmed_incorrect: '明确确认错误',
  conflicting: '多源结果冲突',
  corroborated: '多源结果相互印证',
  consistent: '多源结果一致',
  ocr_only: '仅文字识别获得结果',
  vision_only: '仅视觉模型获得结果',
  insufficient_evidence: '证据不足',
  unresolved: '无法确定',
  unavailable: '服务不可用',
}

export function localizedText(value) {
  if (value === null || value === undefined || value === '') return '未记录'
  return Object.entries(TEXT_ENUM_LABELS).reduce(
    (text, [source, label]) => text.replaceAll(source, label),
    String(value),
  )
}

export class ApiError extends Error {
  constructor(status, code, message, payload = null, fieldErrors = {}, traceId = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.payload = null
    this.fieldErrors = fieldErrors
    this.traceId = traceId
    this.recovery = RECOVERY_MESSAGES[status] || '请检查输入后重试；若问题持续，请使用技术关联标识排查。'
  }
}

export function errorFeedback(error) {
  if (!(error instanceof ApiError)) {
    return { message: '操作未完成，请稍后重试。', traceId: null }
  }
  const trace = error.traceId ? ` 技术关联标识：${error.traceId}` : ''
  return { message: `${error.message}。${error.recovery}${trace}`, traceId: error.traceId }
}

function serializeWriteBody(body) {
  if (!(body instanceof FormData)) return JSON.stringify(body ?? null)
  return JSON.stringify(Array.from(body.entries(), ([key, value]) => [
    key,
    typeof value === 'string'
      ? value
      : { name: value.name, size: value.size, type: value.type, lastModified: value.lastModified },
  ]))
}

async function writeFingerprint(path, method, body) {
  const source = `${method.toUpperCase()}\n${path}\n${serializeWriteBody(body)}`
  const bytes = new TextEncoder().encode(source)
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  return Array.from(new Uint8Array(digest), (item) => item.toString(16).padStart(2, '0')).join('')
}

function cachedWriteKey(fingerprint) {
  const entry = retryableWriteKeys.get(fingerprint)
  if (entry && entry.expiresAt > Date.now()) return entry.key
  retryableWriteKeys.delete(fingerprint)
  const key = crypto.randomUUID()
  retryableWriteKeys.set(fingerprint, { key, expiresAt: Date.now() + WRITE_KEY_TTL_MS })
  return key
}

function queryString(params = {}) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === null || value === undefined || value === '') return
    if (Array.isArray(value)) value.forEach((item) => query.append(key, item))
    else query.set(key, String(value))
  })
  const text = query.toString()
  return text ? `?${text}` : ''
}

export async function apiRequest(path, options = {}) {
  publishRequestActivity(1)
  try {
    let response
    try {
      response = await fetch(path, options)
    } catch {
      throw new ApiError(0, 'network_unavailable', '无法连接服务端', null, {})
    }
    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      const code = payload?.code || `http_${response.status}`
      const message = STATUS_MESSAGES[response.status] || '请求处理失败'
      const fieldErrors = requestValidationFields(payload)
      throw new ApiError(
        response.status,
        code,
        message,
        payload,
        fieldErrors,
        response.headers.get('x-trace-id'),
      )
    }
    return payload
  } finally {
    publishRequestActivity(-1)
  }
}

export function get(path, params) {
  return apiRequest(`${path}${queryString(params)}`)
}

export async function write(path, { method = 'POST', body, headers = {} } = {}) {
  const fingerprint = await writeFingerprint(path, method, body)
  const idempotencyKey = cachedWriteKey(fingerprint)
  const requestHeaders = {
    ...headers,
    'Idempotency-Key': idempotencyKey,
  }
  const options = { method, headers: requestHeaders }
  if (body instanceof FormData) options.body = body
  else if (body !== undefined) {
    requestHeaders['Content-Type'] = 'application/json'
    options.body = JSON.stringify(body)
  }
  try {
    const result = await apiRequest(path, options)
    retryableWriteKeys.delete(fingerprint)
    return result
  } catch (error) {
    if (!(error instanceof ApiError) || ![0, 429, 503].includes(error.status)) {
      retryableWriteKeys.delete(fingerprint)
    }
    throw error
  }
}

export function safeText(value, maxLength = 80) {
  if (value === null || value === undefined || value === '') return '未记录'
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > maxLength ? `${text.slice(0, maxLength)}...` : text
}

export function fullText(value) {
  if (value === null || value === undefined || value === '') return '未记录'
  return typeof value === 'string' ? value : JSON.stringify(value)
}

export function formatDate(value) {
  if (!value) return '未记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false })
}

export function formatScore(value) {
  return Number.isFinite(Number(value)) ? Number(value).toFixed(3) : 'N/A'
}
