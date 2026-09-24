<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { AlertCircle, BrainCircuit, CheckCircle2, FileImage, FileText, LoaderCircle, Play, RefreshCw, Search, ShieldCheck, UploadCloud, X } from 'lucide-vue-next'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { invoiceApi } from '../api/invoice'
import { displayLabel, fullText, localizedText } from '../api/client'
import { buildReviewSubmission, focusReviewTarget, reviewInputMetadata } from './extractionReview'
import { useNotifications } from '../composables/useNotifications'

const emit = defineEmits(['navigate'])
const notices = useNotifications()

const state = reactive({ file: null, document: null, run: null, result: null, review: null, loading: false, error: '', reviewError: '', reviewFeedback: '', activity: '等待上传发票图片或 PDF', backendOnline: false })
const inputRef = ref(null)
const runQuery = ref('')
const reviewValues = reactive({})
const reviewReasons = reactive({})
const reviewActions = reactive({})
const reviewNulls = reactive({})
const reviewFieldErrors = reactive({})
const bindingSelections = reactive({})
const bindingReasons = reactive({})
const reviewTargets = new Map()
let stopped = false

const reviewEntries = computed(() => state.review?.request?.fields || [])
const reviewFields = computed(() => reviewEntries.value.filter((field) => field.field_path))
const reviewNotices = computed(() => reviewEntries.value.filter((field) => !field.field_path))
const reviewBindings = computed(() => state.review?.request?.field_bindings || [])
const extraction = computed(() => state.result?.result || null)
const evidence = computed(() => extraction.value?.field_evidence || [])
const fieldBindingEvidence = computed(() => extraction.value?.field_binding_evidence || [])
const ocrComparisons = computed(() => extraction.value?.ocr_comparisons || [])
const fieldPresence = computed(() => flattenInvoice(extraction.value?.invoice))
const isReview = computed(() => state.run?.status === 'pending_review' && state.review)
const memoryAssist = computed(() => {
  const bindings = fieldBindingEvidence.value
  const normalizedLabels = bindings.filter((item) => item.observed_label && item.normalized_label && item.observed_label !== item.normalized_label)
  const valueSignals = evidence.value.filter((item) => item.candidate_values?.length > 1 || item.validation_signals?.length)
  const corroborated = ocrComparisons.value.filter((item) => ['corroborated', 'consistent'].includes(item.outcome))
  const attention = [
    ...bindings.filter((item) => item.binding_decision?.requires_review),
    ...ocrComparisons.value.filter((item) => item.review_required),
    ...evidence.value.filter((item) => item.ambiguous),
  ]
  return {
    bindings,
    normalizedLabels,
    valueSignals,
    corroborated,
    attention,
    hasEvidence: Boolean(bindings.length || normalizedLabels.length || valueSignals.length || corroborated.length || attention.length),
  }
})
const stages = computed(() => {
  const done = ['pending_review', 'completed'].includes(state.run?.status)
  const hasOcr = Boolean(extraction.value?.ocr_observations?.length || extraction.value?.raw_ocr_observations?.length || state.review?.request?.evidence_sources?.some((item) => item.source_type?.includes('ocr')))
  const hasBinding = Boolean(extraction.value?.field_binding_evidence?.length || reviewBindings.value.length)
  return [
    { label: '视觉识别', state: !state.run ? 'idle' : state.run.status === 'processing' ? 'active' : done ? 'done' : state.run.status },
    { label: '文字识别', state: hasOcr ? 'done' : done ? 'not_reported' : state.run ? 'waiting' : 'idle' },
    { label: '字段绑定', state: hasBinding ? (reviewBindings.value.length ? 'review_required' : 'done') : done ? 'not_reported' : state.run ? 'waiting' : 'idle' },
    { label: '记忆增强', state: memoryAssist.value.hasEvidence ? (memoryAssist.value.attention.length ? 'review_required' : 'done') : done ? 'not_reported' : state.run ? 'waiting' : 'idle' },
  ]
})

function flattenInvoice(value, prefix = '') {
  if (!value || typeof value !== 'object') return []
  if (Array.isArray(value)) {
    if (!value.length) return prefix ? [{ path: prefix, present: false }] : []
    return value.flatMap((item, index) => {
      const path = prefix ? `${prefix}.${index}` : String(index)
      return item && typeof item === 'object'
        ? flattenInvoice(item, path)
        : [{ path, present: item !== null && item !== '', value: item }]
    })
  }
  return Object.entries(value).flatMap(([key, item]) => {
    const path = prefix ? `${prefix}.${key}` : key
    if (item && typeof item === 'object') return flattenInvoice(item, path)
    return [{ path, present: item !== null && item !== '', value: item }]
  })
}
function clearMap(map) { Object.keys(map).forEach((key) => delete map[key]) }
function fieldKey(field) { return field.field_path || 'document' }
function reviewTarget(type, key) { return `${type}:${key}` }
function controlId(type, key, suffix) { return `review-${type}-${String(key).replace(/[^a-zA-Z0-9_-]/g, '-')}-${suffix}` }
function setReviewTarget(key, element) { if (element) reviewTargets.set(key, element); else reviewTargets.delete(key) }
function clearReviewError(key) {
  delete reviewFieldErrors[key]
  if (!Object.keys(reviewFieldErrors).length) state.reviewError = ''
}
function onReviewActionChange(key) {
  if (reviewActions[key] !== 'correct') reviewNulls[key] = false
  clearReviewError(reviewTarget('field', key))
}
function showReviewError(message, target = null, fieldErrors = {}) {
  clearMap(reviewFieldErrors)
  Object.entries(fieldErrors).forEach(([fieldPath, text]) => {
    reviewFieldErrors[reviewTarget('field', fieldPath)] = text
  })
  if (target && !Object.keys(reviewFieldErrors).length) reviewFieldErrors[target] = message
  state.reviewError = message
  const firstTarget = target || Object.keys(reviewFieldErrors)[0]
  if (firstTarget) nextTick(() => focusReviewTarget(firstTarget, (key) => reviewTargets.get(key)))
}
function onFile(event) {
  const file = event.target?.files?.[0] || event.dataTransfer?.files?.[0]
  if (!file) return
  if (!['image/png', 'image/jpeg', 'image/webp', 'application/pdf'].includes(file.type)) {
    state.error = '仅支持 PNG、JPG、WEBP 和 PDF'
    notices.warning('文件格式不支持', '请选择 PNG、JPG、WEBP 图片或 PDF 文件。')
    return
  }
  Object.assign(state, { file, document: null, run: null, result: null, review: null, error: '', activity: '文件已就绪' })
}
function clearFile() { state.file = null; if (inputRef.value) inputRef.value.value = '' }
function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)) }
async function pollRun() {
  const deadline = Date.now() + 180000
  while (!stopped && Date.now() < deadline) {
    state.run = await invoiceApi.run(state.run.run_id)
    if (state.run.status === 'pending_review') {
      state.review = await invoiceApi.review(state.run.run_id)
      hydrateReview()
      state.activity = '当前证据需要人工确认'
      return
    }
    if (state.run.status === 'completed') {
      state.result = await invoiceApi.result(state.run.run_id)
      state.activity = state.run.memory_status === 'failed_retryable' ? '提取完成；记忆任务等待后台恢复' : '提取完成'
      return
    }
    if (state.run.status === 'failed') {
      state.activity = state.run.failure_message || '处理流程失败'
      return
    }
    state.activity = '等待后端报告实际处理状态...'
    await delay(1200)
  }
  if (!stopped) throw new Error('处理超过 3 分钟，请稍后按 run_id 查询')
}
async function start() {
  if (!state.file || state.loading) return
  state.loading = true; state.error = ''; state.activity = '上传并校验文件...'
  try {
    state.document = await invoiceApi.upload(state.file)
    state.activity = '已登记文件，启动确定性处理流程...'
    state.run = await invoiceApi.extract(state.document.document_id)
    await pollRun()
    if (state.run?.status === 'completed') {
      notices.success('发票处理已完成', state.run.memory_status === 'failed_retryable'
        ? '发票结果已保存；记忆任务将由后台恢复，不影响当前业务结果。'
        : '发票结果已保存，可以查看完整字段与证据。')
    } else if (state.run?.status === 'pending_review') {
      notices.warning('需要人工确认', '当前图片证据不足或存在字段冲突，请完成页面中的审核项。', { duration: 8000 })
    } else if (state.run?.status === 'failed') {
      notices.error('发票处理失败', '业务流程未完成，请根据页面提示检查文件或稍后重试。')
    }
  } catch (error) {
    state.error = error.message
    state.activity = '请求失败，可重试'
    notices.error('发票处理未完成', error.recovery ? `${error.message}。${error.recovery}` : '请求未完成，请稍后重试。')
  }
  finally { state.loading = false }
}
async function loadRun() {
  if (!runQuery.value.trim() || state.loading) return
  state.loading = true; state.error = ''
  try {
    state.run = await invoiceApi.run(runQuery.value.trim())
    if (state.run.status === 'completed') state.result = await invoiceApi.result(state.run.run_id)
    if (state.run.status === 'pending_review') { state.review = await invoiceApi.review(state.run.run_id); hydrateReview() }
    state.activity = '已读取后端运行状态'
  } catch (error) { state.error = error.message }
  finally { state.loading = false }
}
function hydrateReview() {
  ;[reviewValues, reviewReasons, reviewActions, reviewNulls, reviewFieldErrors, bindingSelections, bindingReasons].forEach(clearMap)
  state.reviewError = ''
  state.reviewFeedback = ''
  reviewFields.value.forEach((field) => {
    const key = fieldKey(field)
    reviewValues[key] = ''
    reviewReasons[key] = ''
    reviewActions[key] = 'confirm_correct'
    reviewNulls[key] = false
  })
  reviewBindings.value.forEach((item) => { bindingSelections[item.evidence_id] = ''; bindingReasons[item.evidence_id] = '' })
}
async function submitReview() {
  if (!isReview.value) return
  state.reviewError = ''; state.reviewFeedback = ''
  clearMap(reviewFieldErrors)
  let payload
  try {
    payload = buildReviewSubmission({
      currentInvoice: state.review.current_invoice,
      reviewFields: reviewFields.value,
      reviewBindings: reviewBindings.value,
      reviewActions,
      reviewValues,
      reviewReasons,
      bindingSelections,
      bindingReasons,
      reviewNulls,
    })
  } catch (error) {
    showReviewError(error.message, error.target)
    state.activity = '审核内容尚未满足提交要求'
    notices.warning('审核内容不完整', error.message)
    return
  }
  state.loading = true; state.error = ''; state.activity = '正在提交审核决定...'
  try {
    state.run = await invoiceApi.submitReview(state.run.run_id, payload)
    state.review = null
    await pollRun()
    if (state.run?.status === 'pending_review') {
      const reason = reviewNotices.value.at(-1)?.reasons?.at(-1)
      state.reviewFeedback = reason
        ? `审核已提交，但后端仍要求处理：${localizedText(reason)}`
        : '审核已提交，但仍有未解决项。请检查当前审核字段后再次提交。'
      state.activity = '审核已接收，仍需处理未解决项'
      notices.warning('审核已保存，仍有待确认项', state.reviewFeedback, { duration: 9000 })
    } else if (state.run?.status === 'completed') {
      notices.success('审核结果已保存', '发票业务结果已完成；记忆候选将由独立准入流程处理。', {
        actionLabel: '查看记忆准入',
        onAction: () => emit('navigate', 'admissions'),
      })
    }
  } catch (error) {
    showReviewError(error.message, null, error.fieldErrors)
    state.activity = '审核提交失败，可修正后重试'
    notices.error('审核提交未完成', error.recovery ? `${error.message}。${error.recovery}` : '审核尚未保存，请修正后重试。')
  }
  finally { state.loading = false }
}
onMounted(async () => { try { await invoiceApi.health(); state.backendOnline = true } catch { state.backendOnline = false } })
onBeforeUnmount(() => { stopped = true })
</script>

<template>
  <div>
    <PageHeader eyebrow="提取工作台" title="发票提取工作台" description="上传、提取、证据校验与显式人工审核。">
      <span class="service-state" :class="{ online: state.backendOnline }"><i />{{ state.backendOnline ? '后端在线' : '后端未连接' }}</span>
    </PageHeader>

    <section class="stage-strip">
      <div v-for="item in stages" :key="item.label" class="stage-cell"><span>{{ item.label }}</span><StatusBadge :value="item.state" /></div>
      <div class="stage-note"><LoaderCircle v-if="state.loading" class="spin" :size="18" /><CheckCircle2 v-else :size="18" /><span>{{ state.activity }}</span></div>
    </section>

    <div v-if="state.error" class="alert error global-alert" role="alert"><AlertCircle :size="18" /><span>{{ state.error }}</span><button class="icon-btn" title="关闭" @click="state.error = ''"><X :size="16" /></button></div>

    <div class="workbench-grid">
      <section class="surface upload-surface">
        <header class="section-head"><div><span>文件输入</span><h2>上传文件</h2></div><FileImage :size="19" /></header>
        <input ref="inputRef" hidden type="file" accept="image/png,image/jpeg,image/webp,application/pdf" @change="onFile" />
        <button class="drop-zone" @click="inputRef?.click()" @dragover.prevent @drop.prevent="onFile">
          <UploadCloud :size="27" /><strong>{{ state.file?.name || '选择或拖拽文件' }}</strong><small>{{ state.file ? `${Math.ceil(state.file.size / 1024)} KB` : 'PNG · JPG · WEBP · PDF' }}</small>
        </button>
        <div class="button-row"><button v-if="state.file" class="secondary-btn" @click="clearFile"><X :size="16" />移除</button><button class="primary-btn grow" :disabled="!state.file || state.loading" @click="start"><LoaderCircle v-if="state.loading" class="spin" :size="16" /><Play v-else :size="16" />开始提取</button></div>
        <div class="lookup"><label>按 run_id 查询<input v-model="runQuery" placeholder="run_..." @keyup.enter="loadRun" /></label><button class="icon-btn" title="查询运行" :disabled="state.loading" @click="loadRun"><Search :size="17" /></button></div>
        <dl v-if="state.run" class="meta-list"><div><dt>运行编号</dt><dd>{{ state.run.run_id }}</dd></div><div><dt>处理状态</dt><dd><StatusBadge :value="state.run.status" /></dd></div><div><dt>校验结果</dt><dd><StatusBadge v-if="state.run.validation_route" :value="state.run.validation_route" /><span v-else>未报告</span></dd></div><div><dt>记忆状态</dt><dd><StatusBadge v-if="state.run.memory_status" :value="state.run.memory_status" /><span v-else>未创建</span></dd></div><div v-if="state.run.memory_error_code"><dt>恢复错误码</dt><dd>{{ state.run.memory_error_code }}</dd></div></dl>
      </section>

      <section class="surface result-surface">
        <header class="section-head"><div><span>提取结果</span><h2>结构化结果与证据</h2></div><RefreshCw :size="18" /></header>
        <div v-if="!extraction && !isReview" class="empty-inline"><FileText :size="28" /><strong>等待后端响应</strong><span>不会虚构 OCR、Retrieval 或字段绑定进度。</span></div>
        <template v-if="extraction">
          <div class="metric-row"><div><span>字段总数</span><strong>{{ fieldPresence.length }}</strong></div><div><span>已识别</span><strong>{{ fieldPresence.filter((item) => item.present).length }}</strong></div><div><span>异常</span><strong>{{ extraction.anomalies?.length || 0 }}</strong></div><div><span>字段证据</span><strong>{{ evidence.length }}</strong></div></div>
          <section class="memory-assist-panel" aria-labelledby="memory-assist-title">
            <header class="memory-assist-head">
              <div><BrainCircuit :size="18" /><div><span>记忆增强</span><h3 id="memory-assist-title">字段表达与数据判断</h3></div></div>
              <StatusBadge :value="memoryAssist.attention.length ? 'review_required' : memoryAssist.hasEvidence ? 'done' : 'not_reported'" />
            </header>
            <p class="memory-assist-summary">
              {{ memoryAssist.hasEvidence
                ? `已依据字段语义、规范化表达和多源值判断生成当前结果${memoryAssist.attention.length ? '；仍有内容需要人工确认' : '，当前未发现需要升级的记忆冲突'}。`
                : '当前响应未返回可展示的记忆增强证据，不以推测代替后端状态。' }}
            </p>
            <div v-if="memoryAssist.hasEvidence" class="memory-assist-grid">
              <article v-if="memoryAssist.normalizedLabels.length" class="memory-assist-card">
                <strong>表达泛化</strong>
                <div v-for="item in memoryAssist.normalizedLabels.slice(0, 4)" :key="item.evidence_id" class="memory-assist-line">
                  <span>{{ fullText(item.observed_label) }}</span><b>→</b><code>{{ fullText(item.normalized_label) }}</code>
                </div>
              </article>
              <article v-if="memoryAssist.valueSignals.length" class="memory-assist-card">
                <strong>值判断</strong>
                <div v-for="item in memoryAssist.valueSignals.slice(0, 4)" :key="item.field_path" class="memory-assist-line stacked">
                  <code>{{ item.field_path }}</code><span>{{ localizedText(item.validation_signals?.join(' · ') || `发现 ${item.candidate_values.length} 个候选值`) }}</span>
                </div>
              </article>
              <article v-if="memoryAssist.corroborated.length" class="memory-assist-card">
                <strong>多源印证</strong>
                <div v-for="item in memoryAssist.corroborated.slice(0, 4)" :key="item.canonical_field_path" class="memory-assist-line stacked">
                  <code>{{ item.canonical_field_path }}</code><span>{{ localizedText(item.reason_codes?.join(' · ') || '视觉与文字识别结果一致') }}</span>
                </div>
              </article>
            </div>
            <div v-if="memoryAssist.attention.length" class="memory-assist-warning"><AlertCircle :size="15" /><span>记忆只提供候选和提醒，当前证据冲突或字段不确定时仍需人工确认。</span></div>
          </section>
          <h3 class="subheading">完整字段结果</h3>
          <div class="field-grid"><div v-for="field in fieldPresence" :key="field.path" class="field-value-row"><code>{{ field.path }}</code><strong>{{ fullText(field.value) }}</strong><StatusBadge :value="field.present ? 'completed' : 'unresolved'" /></div></div>
          <h3 class="subheading">字段证据</h3>
          <div class="record-list"><article v-for="item in evidence" :key="item.field_path"><header><code>{{ item.field_path }}</code><span>{{ displayLabel(item.source, 'evidence_source') }} · 第 {{ item.page_number || '-' }} 页</span></header><p>{{ localizedText((item.validation_signals || []).join(' · ') || '无额外验证信号') }}</p><footer>候选 {{ item.candidate_values?.length || 0 }} 个 · 可读性 {{ item.readability || '未记录' }}</footer></article></div>
        </template>

        <div v-if="isReview" class="review-zone">
          <div class="review-title"><ShieldCheck :size="19" /><div><strong>需要人工审核</strong><span>只有主动选择“明确确认正确”，系统才会记录为已确认正确。</span></div></div>
          <p class="trusted-actor-note"><ShieldCheck :size="16" />审核人由后端可信身份上下文确定，页面不能修改。</p>
          <article v-for="(notice, index) in reviewNotices" :key="`document-${index}`" class="review-item"><header><strong>整份文档</strong></header><p>{{ localizedText(notice.reasons?.join('；') || '请重新提交字段级审核。') }}</p></article>
          <article
            v-for="field in reviewFields"
            :key="fieldKey(field)"
            :ref="(element) => setReviewTarget(reviewTarget('field', fieldKey(field)), element)"
            class="review-item"
            :class="{ 'review-item-error': reviewFieldErrors[reviewTarget('field', fieldKey(field))] }"
          >
            <header><code>{{ fieldKey(field) }}</code><span class="full-value">{{ fullText(field.current_value) }}</span></header>
            <p>{{ localizedText(field.reasons?.join('；') || '后端要求人工判断') }}</p>
            <div class="review-controls">
              <label :for="controlId('field', fieldKey(field), 'action')">审核决定</label>
              <select
                :id="controlId('field', fieldKey(field), 'action')"
                v-model="reviewActions[fieldKey(field)]"
                data-review-control
                @change="onReviewActionChange(fieldKey(field))"
              ><option value="confirm_correct">明确确认正确</option><option value="correct">人工修正</option><option value="confirm_incorrect">明确否定</option></select>
              <template v-if="reviewActions[fieldKey(field)] === 'correct'">
                <label :for="controlId('field', fieldKey(field), 'value')">修正值</label>
                <input
                  :id="controlId('field', fieldKey(field), 'value')"
                  v-model="reviewValues[fieldKey(field)]"
                  data-review-control
                  :type="reviewInputMetadata(fieldKey(field)).type"
                  :step="reviewInputMetadata(fieldKey(field)).type === 'datetime-local' ? 1 : undefined"
                  :placeholder="reviewInputMetadata(fieldKey(field)).hint"
                  :disabled="reviewNulls[fieldKey(field)]"
                  :aria-invalid="Boolean(reviewFieldErrors[reviewTarget('field', fieldKey(field))])"
                  :aria-describedby="reviewFieldErrors[reviewTarget('field', fieldKey(field))] ? controlId('field', fieldKey(field), 'error') : undefined"
                  @input="clearReviewError(reviewTarget('field', fieldKey(field)))"
                />
                <label class="review-null-option"><input v-model="reviewNulls[fieldKey(field)]" type="checkbox" @change="clearReviewError(reviewTarget('field', fieldKey(field)))" />设为空</label>
              </template>
              <label :for="controlId('field', fieldKey(field), 'reason')">审核原因</label>
              <input
                :id="controlId('field', fieldKey(field), 'reason')"
                v-model="reviewReasons[fieldKey(field)]"
                placeholder="修正或否定时必填"
                @input="clearReviewError(reviewTarget('field', fieldKey(field)))"
              />
            </div>
            <p
              v-if="reviewFieldErrors[reviewTarget('field', fieldKey(field))]"
              :id="controlId('field', fieldKey(field), 'error')"
              class="review-field-error"
            >{{ reviewFieldErrors[reviewTarget('field', fieldKey(field))] }}</p>
          </article>
          <article
            v-for="binding in reviewBindings"
            :key="binding.evidence_id"
            :ref="(element) => setReviewTarget(reviewTarget('binding', binding.evidence_id), element)"
            class="review-item binding"
            :class="{ 'review-item-error': reviewFieldErrors[reviewTarget('binding', binding.evidence_id)] }"
          >
            <header><strong>{{ fullText(binding.observed_label) }}</strong><StatusBadge :value="binding.binding_decision?.status || 'review_required'" /></header>
            <p>原始标签已规范化为 {{ fullText(binding.normalized_label) }}；仅可选择当前数据结构中声明的候选字段。</p>
            <div class="review-controls"><select v-model="bindingSelections[binding.evidence_id]" data-review-control @change="clearReviewError(reviewTarget('binding', binding.evidence_id))"><option value="">选择标准字段路径</option><option v-for="path in binding.candidate_field_paths" :key="path" :value="path">{{ path }}</option></select><input v-model="bindingReasons[binding.evidence_id]" placeholder="字段映射确认原因" @input="clearReviewError(reviewTarget('binding', binding.evidence_id))" /></div>
            <p v-if="reviewFieldErrors[reviewTarget('binding', binding.evidence_id)]" class="review-field-error">{{ reviewFieldErrors[reviewTarget('binding', binding.evidence_id)] }}</p>
          </article>
          <div v-if="state.reviewError" class="alert error review-submit-feedback" role="alert" aria-live="assertive"><AlertCircle :size="18" /><span>{{ state.reviewError }}</span></div>
          <div v-if="state.reviewFeedback" class="alert neutral review-submit-feedback" role="status" aria-live="polite"><ShieldCheck :size="18" /><span>{{ state.reviewFeedback }}</span></div>
          <button type="button" class="primary-btn" :disabled="state.loading" @click="submitReview"><LoaderCircle v-if="state.loading" class="spin" :size="16" /><ShieldCheck v-else :size="16" />{{ state.loading ? '正在提交...' : '提交审核' }}</button>
        </div>
      </section>
    </div>
  </div>
</template>
