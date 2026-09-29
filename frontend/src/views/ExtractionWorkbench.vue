<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { AlertCircle, BrainCircuit, CheckCircle2, FileImage, FileText, List, LoaderCircle, Play, RefreshCw, Search, ShieldCheck, UploadCloud, X } from 'lucide-vue-next'
import RunFieldsDialog from '../components/RunFieldsDialog.vue'
import InvoiceBatchWorkbench from '../components/InvoiceBatchWorkbench.vue'
import PageHeader from '../components/PageHeader.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { invoiceApi } from '../api/invoice'
import { displayLabel, errorFeedback, formatFieldValue, fullText, localizedText } from '../api/client'
import { buildReviewSubmission, buildReviewSubmissionEnvelope, focusReviewTarget, reviewInputMetadata } from './extractionReview'
import { useNotifications } from '../composables/useNotifications'

const emit = defineEmits(['navigate'])
const notices = useNotifications()

const state = reactive({
  file: null, document: null, pendingExtract: null, run: null, result: null, reviewTask: null, leaseToken: null,
  reviewClaiming: false, reviewSubmitting: false, readOnlyReviewReason: '',
  loading: false, error: '', reviewError: '', reviewFeedback: '',
  activity: '等待上传发票图片或 PDF', backendOnline: false,
})
const inputRef = ref(null)
const extractionMode = ref('single')
const runQuery = ref('')
const recentRuns = ref([])
const recentRunsLoading = ref(false)
const recentRunsError = ref('')
const runDialogOpen = ref(false)
const reviewValues = reactive({})
const reviewReasons = reactive({})
const reviewActions = reactive({})
const reviewNulls = reactive({})
const reviewFieldErrors = reactive({})
const bindingSelections = reactive({})
const bindingReasons = reactive({})
const reviewTargets = new Map()
let stopped = false
let runGeneration = 0
const recentRunKey = 'invoice-intelligence-recent-run-id'
const pendingExtractKey = 'invoice-intelligence-pending-extract'
function savePendingExtract(value) {
  state.pendingExtract = value
  try {
    if (value) sessionStorage.setItem(pendingExtractKey, JSON.stringify(value))
    else sessionStorage.removeItem(pendingExtractKey)
  } catch {}
}
function rememberRun(runId) {
  runQuery.value = runId
  if (state.pendingExtract?.documentId === state.run?.document_id) savePendingExtract(null)
  try { sessionStorage.setItem(recentRunKey, runId) } catch {}
}
async function loadRecentRuns() {
  recentRunsLoading.value = true
  recentRunsError.value = ''
  try { recentRuns.value = await invoiceApi.recentRuns() }
  catch (error) { recentRunsError.value = errorFeedback(error).message }
  finally { recentRunsLoading.value = false }
}
function selectRecentRun(runId) {
  runQuery.value = runId
  void loadRun()
}

const reviewEntries = computed(() => state.reviewTask?.request?.fields || [])
const reviewFields = computed(() => reviewEntries.value.filter((field) => field.field_path))
const reviewNotices = computed(() => reviewEntries.value.filter((field) => !field.field_path))
const reviewBindings = computed(() => state.reviewTask?.request?.field_bindings || [])
const reviewEvidence = computed(() => state.reviewTask?.request?.evidence_sources || [])
const extraction = computed(() => state.result?.result || null)
const evidence = computed(() => extraction.value?.field_evidence || [])
const fieldBindingEvidence = computed(() => extraction.value?.field_binding_evidence || [])
const ocrComparisons = computed(() => extraction.value?.ocr_comparisons || [])
const fieldPresence = computed(() => flattenInvoice(extraction.value?.invoice))
const isReview = computed(() => state.run?.status === 'pending_review' && state.reviewTask)
const canSubmitReview = computed(() => isReview.value && state.reviewTask?.status === 'claimed' && state.leaseToken && !state.reviewSubmitting)
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
  const hasOcr = Boolean(extraction.value?.ocr_observations?.length || extraction.value?.raw_ocr_observations?.length || state.reviewTask?.request?.evidence_sources?.some((item) => item.source_type?.includes('ocr')))
  const hasBinding = Boolean(extraction.value?.field_binding_evidence?.length || reviewBindings.value.length)
  return [
    { label: '视觉识别', state: !state.run ? 'idle' : state.run.status === 'processing' ? 'active' : done ? 'done' : state.run.status },
    { label: '文字识别', state: hasOcr ? 'done' : done ? 'not_reported' : state.run ? 'waiting' : 'idle' },
    { label: '字段绑定', state: hasBinding ? (reviewBindings.value.length ? 'review_required' : 'done') : done ? 'not_reported' : state.run ? 'waiting' : 'idle' },
    { label: '当前证据', state: memoryAssist.value.hasEvidence ? (memoryAssist.value.attention.length ? 'review_required' : 'done') : done ? 'not_reported' : state.run ? 'waiting' : 'idle' },
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
  if (state.pendingExtract || state.loading) return
  const file = event.target?.files?.[0] || event.dataTransfer?.files?.[0]
  if (!file) return
  if (!['image/png', 'image/jpeg', 'image/webp', 'application/pdf'].includes(file.type)) {
    state.error = '仅支持 PNG、JPG、WEBP 和 PDF'
    notices.warning('文件格式不支持', '请选择 PNG、JPG、WEBP 图片或 PDF 文件。')
    return
  }
  runGeneration++
  Object.assign(state, {
    file, document: null, run: null, result: null, reviewTask: null, leaseToken: null,
    readOnlyReviewReason: '', error: '', activity: '文件已就绪',
  })
}
function clearFile() { state.file = null; if (inputRef.value) inputRef.value.value = '' }
function abandonPendingExtract() {
  savePendingExtract(null)
  state.document = null
  state.activity = '已放弃恢复信息；重新上传可能创建新任务'
}
function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)) }
async function pollRun(runId, generation, shouldClaim = false) {
  while (!stopped && generation === runGeneration) {
    let run
    try { run = await invoiceApi.run(runId) }
    catch (error) {
      if (![0, 503].includes(error.status)) throw error
      state.activity = '暂时无法读取运行状态，正在重试；运行编号已保留'
      await delay(3000)
      continue
    }
    if (generation !== runGeneration || stopped) return
    state.run = run
    if (state.run.status === 'pending_review') {
      await loadReviewTask(shouldClaim)
      state.activity = '当前证据需要人工确认'
      return
    }
    if (state.run.status === 'completed') {
      await loadCompletedResult(runId, generation)
      return
    }
    if (state.run.status === 'failed') {
      state.activity = state.run.failure_message || '处理流程失败'
      return
    }
    if (!['received', 'processing'].includes(state.run.status)) {
      state.activity = '后端返回了暂不支持的运行状态，请按 run_id 重新读取'
      return
    }
    state.activity = state.run.status === 'received' ? '已接收，等待处理...' : '正在处理发票...'
    await delay(1200)
  }
}
async function loadCompletedResult(runId, generation) {
  try {
    const result = await invoiceApi.result(runId)
    if (generation !== runGeneration || stopped) return
    state.result = result
    state.activity = state.run.memory_status === 'failed_retryable' ? '提取完成；记忆任务等待后台恢复' : '提取完成'
  } catch (error) {
    if (generation !== runGeneration || stopped) return
    state.error = errorFeedback(error).message
    state.activity = '提取已完成，结果暂不可读取；请按运行编号重试'
  }
}
async function start() {
  if ((!state.file && !state.pendingExtract) || state.loading) return
  state.loading = true; state.error = ''; state.activity = '上传并校验文件...'
  try {
    if (!state.pendingExtract) {
      state.document = await invoiceApi.upload(state.file)
      savePendingExtract({ documentId: state.document.document_id, idempotencyKey: crypto.randomUUID() })
    }
    state.activity = '已登记文件，启动确定性处理流程...'
    state.run = await invoiceApi.extract(state.pendingExtract.documentId, state.pendingExtract.idempotencyKey)
    rememberRun(state.run.run_id)
    void loadRecentRuns()
    const generation = ++runGeneration
    state.result = null
    clearReviewState()
    await pollRun(state.run.run_id, generation, true)
    if (state.run?.status === 'completed' && state.result) {
      notices.success('发票处理已完成', state.run.memory_status === 'failed_retryable'
        ? '发票结果已保存；记忆任务将由后台恢复，不影响当前业务结果。'
        : '发票结果已保存，可以查看完整字段与证据。')
    } else if (state.run?.status === 'pending_review') {
      notices.warning('需要人工确认', '当前图片证据不足或存在字段冲突，请完成页面中的审核项。', { duration: 8000 })
    } else if (state.run?.status === 'failed') {
      notices.error('发票处理失败', '业务流程未完成，请根据页面提示检查文件或稍后重试。')
    } else if (state.run?.status === 'completed') {
      notices.warning('提取已完成', '结果暂不可读取，请按运行编号重新查询。')
    }
  } catch (error) {
    state.error = errorFeedback(error).message
    state.activity = state.pendingExtract ? '提取请求状态未知，可使用原请求安全重试' : '请求失败，可重试'
    notices.error('发票处理未完成', state.error)
  }
  finally { state.loading = false }
}
async function loadRun() {
  if (!runQuery.value.trim() || state.loading) return
  state.loading = true; state.error = ''
  const generation = ++runGeneration
  const runId = runQuery.value.trim()
  state.result = null
  clearReviewState()
  try {
    const run = await invoiceApi.run(runId)
    if (generation !== runGeneration || stopped) return
    state.run = run
    rememberRun(state.run.run_id)
    if (state.run.status === 'completed') await loadCompletedResult(runId, generation)
    if (state.run.status === 'pending_review') await loadReviewTask(false)
    if (state.run.status === 'failed') state.activity = state.run.failure_message || '处理流程失败'
    else if (state.run.status !== 'completed') state.activity = '已读取后端运行状态'
    if (['received', 'processing'].includes(state.run.status)) {
      void pollRun(runId, generation).catch((error) => { if (generation === runGeneration) state.error = errorFeedback(error).message })
    }
  } catch (error) { state.error = errorFeedback(error).message }
  finally { state.loading = false }
}
function clearReviewState() {
  state.reviewTask = null
  state.leaseToken = null
  state.readOnlyReviewReason = ''
  ;[reviewValues, reviewReasons, reviewActions, reviewNulls, reviewFieldErrors, bindingSelections, bindingReasons].forEach(clearMap)
}
async function loadReviewTask(shouldClaim) {
  const identifier = state.run?.run_id
  if (!identifier) return
  const task = await invoiceApi.review(identifier)
  if (state.run?.run_id !== identifier || stopped) return
  state.reviewTask = task
  state.leaseToken = null
  state.readOnlyReviewReason = ''
  hydrateReview()
  if (!shouldClaim) {
    if (task.status === 'claimed') state.readOnlyReviewReason = '该审核任务已被领取，当前页面没有可恢复的租约。请重新读取，待租约过期后再领取。'
    else if (['submitted', 'cancelled'].includes(task.status)) state.readOnlyReviewReason = `审核任务已${task.status === 'submitted' ? '提交' : '取消'}，不能重复提交审核决定。`
    return
  }
  if (!['pending_review', 'expired'].includes(task.status)) {
    if (task.status === 'claimed') state.readOnlyReviewReason = '该审核任务已被其他页面领取，当前页面只读等待租约释放。'
    else if (['submitted', 'cancelled'].includes(task.status)) state.readOnlyReviewReason = '审核任务已完成，不能重复提交审核决定。'
    return
  }
  await claimReviewTask()
}
async function claimReviewTask() {
  if (!state.reviewTask || !['pending_review', 'expired'].includes(state.reviewTask.status) || state.reviewClaiming) return
  state.reviewClaiming = true
  state.readOnlyReviewReason = ''
  try {
    const response = await invoiceApi.claimReview(state.reviewTask.review_id, state.reviewTask.revision)
    if (state.run?.run_id !== response.task.run_id || stopped) return
    state.reviewTask = response.task
    state.leaseToken = response.lease_token || null
    if (!state.leaseToken) {
      state.readOnlyReviewReason = '后端未返回审核租约，当前页面只读，不能提交。'
      return
    }
    hydrateReview()
    state.activity = '审核任务已领取，可提交审核决定'
  } catch (error) {
    if (error.status === 409) {
      clearReviewState()
      await reloadReviewAfterConflict()
      return
    }
    state.readOnlyReviewReason = error.message
    throw error
  } finally {
    state.reviewClaiming = false
  }
}
async function reloadReviewAfterConflict() {
  if (!state.run?.run_id) return
  const runId = state.run.run_id
  const generation = runGeneration
  const run = await invoiceApi.run(runId)
  if (generation !== runGeneration || stopped) return
  state.run = run
  if (state.run.status === 'pending_review') await loadReviewTask(false)
  else if (state.run.status === 'completed') await loadCompletedResult(runId, generation)
  else if (['received', 'processing'].includes(state.run.status)) void pollRun(runId, generation)
  state.activity = '已读取最新审核状态'
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
  if (!canSubmitReview.value) return
  state.reviewError = ''; state.reviewFeedback = ''
  clearMap(reviewFieldErrors)
  let payload
  try {
    payload = buildReviewSubmission({
      currentInvoice: state.reviewTask.current_invoice,
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
  state.loading = true; state.reviewSubmitting = true; state.error = ''; state.activity = '正在提交审核决定...'
  try {
    const response = await invoiceApi.submitClaimedReview(
      state.reviewTask.review_id,
      buildReviewSubmissionEnvelope({
        expectedRevision: state.reviewTask.revision,
        leaseToken: state.leaseToken,
        correction: payload,
      }),
    )
    state.run = response.run
    rememberRun(state.run.run_id)
    state.reviewTask = response.task
    state.leaseToken = null
    clearMap(reviewFieldErrors)
    await pollRun(state.run.run_id, runGeneration, true)
    if (state.run?.status === 'pending_review') {
      const reason = reviewNotices.value.at(-1)?.reasons?.at(-1)
      state.reviewFeedback = reason
        ? `审核已提交，但后端仍要求处理：${localizedText(reason)}`
        : '审核已提交，但仍有未解决项。请检查当前审核字段后再次提交。'
      state.activity = '审核已接收，仍需处理未解决项'
      notices.warning('审核已保存，仍有待确认项', state.reviewFeedback, { duration: 9000 })
    } else if (state.run?.status === 'completed' && state.result) {
      notices.success('审核结果已保存', '发票业务结果已完成；记忆候选将由独立准入流程处理。', {
        actionLabel: '查看记忆准入',
        onAction: () => emit('navigate', 'admissions'),
      })
    } else if (state.run?.status === 'completed') {
      notices.warning('审核已保存', '业务结果暂不可读取，请按同一运行编号重新查询。')
    }
  } catch (error) {
    if (error.status === 409) {
      clearReviewState()
      try { await reloadReviewAfterConflict() } catch (refreshError) { state.error = refreshError.message }
    }
    showReviewError(error.message, null, error.fieldErrors)
    state.activity = '审核提交失败，可修正后重试'
    notices.error('审核提交未完成', error.recovery ? `${error.message}。${error.recovery}` : '审核尚未保存，请修正后重试。')
  }
  finally {
    state.reviewSubmitting = false
    state.loading = false
  }
}
onMounted(async () => {
  void loadRecentRuns()
  try { await invoiceApi.health(); state.backendOnline = true } catch { state.backendOnline = false }
  let pending = null
  try { pending = JSON.parse(sessionStorage.getItem(pendingExtractKey) || 'null') } catch {}
  try {
    if (pending && typeof pending.documentId === 'string' && typeof pending.idempotencyKey === 'string') {
      state.pendingExtract = pending
      state.activity = '已恢复待确认的提取请求，可用原请求继续'
      return
    }
    const recentRun = sessionStorage.getItem(recentRunKey)
    if (recentRun && !state.run) { runQuery.value = recentRun; await loadRun() }
  } catch {}
})
onBeforeUnmount(() => { stopped = true; runGeneration++ })
</script>

<template>
  <div>
    <PageHeader eyebrow="提取工作台" title="发票提取工作台" description="上传、提取、证据校验与显式人工审核。">
      <span class="service-state" :class="{ online: state.backendOnline }"><i />{{ state.backendOnline ? '后端在线' : '后端未连接' }}</span>
    </PageHeader>
    <div class="extraction-mode" role="group" aria-label="提取模式"><button type="button" :aria-pressed="extractionMode === 'single'" @click="extractionMode = 'single'">单张发票</button><button type="button" :aria-pressed="extractionMode === 'batch'" @click="extractionMode = 'batch'">多张发票</button></div>
    <InvoiceBatchWorkbench v-if="extractionMode === 'batch'" />
    <template v-if="extractionMode === 'single'">

    <section class="stage-strip">
      <div v-for="item in stages" :key="item.label" class="stage-cell"><span>{{ item.label }}</span><StatusBadge :value="item.state" /></div>
      <div class="stage-note"><LoaderCircle v-if="state.loading" class="spin" :size="18" /><CheckCircle2 v-else :size="18" /><span>{{ state.activity }}</span></div>
    </section>

    <div v-if="state.error" class="alert error global-alert" role="alert"><AlertCircle :size="18" /><span>{{ state.error }}</span><button class="icon-btn" title="关闭" @click="state.error = ''"><X :size="16" /></button></div>

    <div class="workbench-grid">
      <section class="surface upload-surface">
        <header class="section-head"><div><span>文件输入</span><h2>上传文件</h2></div><FileImage :size="19" /></header>
        <input ref="inputRef" hidden type="file" accept="image/png,image/jpeg,image/webp,application/pdf" @change="onFile" />
        <button class="drop-zone" :disabled="Boolean(state.pendingExtract) || state.loading" @click="inputRef?.click()" @dragover.prevent @drop.prevent="onFile">
          <UploadCloud :size="27" /><strong>{{ state.file?.name || '选择或拖拽文件' }}</strong><small>{{ state.file ? `${Math.ceil(state.file.size / 1024)} KB` : 'PNG · JPG · WEBP · PDF' }}</small>
        </button>
        <div class="button-row"><button v-if="state.file" class="secondary-btn" @click="clearFile"><X :size="16" />移除</button><button class="primary-btn grow" :disabled="(!state.file && !state.pendingExtract) || state.loading" @click="start"><LoaderCircle v-if="state.loading" class="spin" :size="16" /><Play v-else :size="16" />{{ state.pendingExtract ? '继续提取请求' : '开始提取' }}</button></div>
        <p v-if="state.pendingExtract" class="context-note">文件已登记。继续时会复用原提取请求，不会创建第二个任务。<button class="secondary-btn" type="button" @click="abandonPendingExtract"><X :size="14" />放弃恢复</button></p>
        <div class="lookup"><label>按 run_id 查询<input v-model="runQuery" placeholder="run_..." @keyup.enter="loadRun" /></label><button class="icon-btn" title="查询运行" :disabled="state.loading" @click="loadRun"><Search :size="17" /></button></div>
        <div class="recent-ids"><div class="recent-ids-head"><strong>我的最近 Run</strong><button class="icon-btn" type="button" title="刷新最近 Run" :disabled="recentRunsLoading" @click="loadRecentRuns"><RefreshCw :size="15" /></button></div><p v-if="recentRunsError" role="alert">{{ recentRunsError }}</p><p v-else-if="recentRunsLoading && !recentRuns.length">正在加载...</p><p v-else-if="!recentRuns.length">暂无可查询的 Run</p><ul v-else><li v-for="item in recentRuns" :key="item.run_id"><button type="button" :disabled="state.loading" @click="selectRecentRun(item.run_id)"><code>{{ item.run_id }}</code><small>{{ new Date(item.created_at).toLocaleString('zh-CN') }}</small><StatusBadge :value="item.status" /></button></li></ul></div>
        <dl v-if="state.run" class="meta-list"><div><dt>运行编号</dt><dd><code>{{ state.run.run_id }}</code></dd></div><div><dt>处理状态</dt><dd><StatusBadge :value="state.run.status" /></dd></div><div><dt>校验结果</dt><dd><StatusBadge v-if="state.run.validation_route" :value="state.run.validation_route" /><span v-else>未报告</span></dd></div><div><dt>记忆状态</dt><dd><StatusBadge v-if="state.run.memory_status" :value="state.run.memory_status" /><span v-else>未创建</span></dd></div></dl>
        <details v-if="state.run" class="technical-details">
          <summary>查看运行与恢复详情</summary>
          <dl class="meta-list">
            <div><dt>运行编号</dt><dd>{{ state.run.run_id }}</dd></div>
            <div v-if="state.run.memory_error_code"><dt>恢复错误码</dt><dd>{{ state.run.memory_error_code }}</dd></div>
            <div><dt>技术状态</dt><dd>{{ state.run.status || '未报告' }}</dd></div>
          </dl>
        </details>
      </section>

      <section class="surface result-surface">
        <header class="section-head"><div><span>提取结果</span><h2>结构化结果与证据</h2></div><button v-if="state.run?.status === 'completed'" class="secondary-btn" @click="runDialogOpen = true"><List :size="16" />查看本 Run 全部字段</button></header>
        <div v-if="!extraction && !isReview" class="empty-inline"><FileText :size="28" /><strong>等待后端响应</strong><span>不会虚构 OCR、Retrieval 或字段绑定进度。</span></div>
        <template v-if="extraction">
          <div class="metric-row"><div><span>字段总数</span><strong>{{ fieldPresence.length }}</strong></div><div><span>已识别</span><strong>{{ fieldPresence.filter((item) => item.present).length }}</strong></div><div><span>异常</span><strong>{{ extraction.anomalies?.length || 0 }}</strong></div><div><span>字段证据</span><strong>{{ evidence.length }}</strong></div></div>
          <section class="memory-assist-panel" aria-labelledby="memory-assist-title">
            <header class="memory-assist-head">
              <div><BrainCircuit :size="18" /><div><span>当前图片证据</span><h3 id="memory-assist-title">字段表达与多源判断</h3></div></div>
              <StatusBadge :value="memoryAssist.attention.length ? 'review_required' : memoryAssist.hasEvidence ? 'done' : 'not_reported'" />
            </header>
            <p class="memory-assist-summary">
              {{ memoryAssist.hasEvidence
                ? `当前结果包含字段表达或多源证据${memoryAssist.attention.length ? '；仍有内容需要人工确认' : '。'}。这些信号不代表记忆已带来识别收益。`
                : '当前响应未返回可展示的字段或 OCR 证据。' }}
            </p>
            <div v-if="memoryAssist.hasEvidence" class="memory-assist-grid">
              <article v-if="memoryAssist.normalizedLabels.length" class="memory-assist-card">
                <strong>标签规范化</strong>
                <div v-for="item in memoryAssist.normalizedLabels.slice(0, 4)" :key="item.evidence_id" class="memory-assist-line">
                  <span>{{ fullText(item.observed_label) }}</span><b>→</b><code>{{ fullText(item.normalized_label) }}</code>
                </div>
              </article>
              <article v-if="memoryAssist.valueSignals.length" class="memory-assist-card">
                <strong>候选与校验</strong>
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
          <div class="field-grid"><div v-for="field in fieldPresence" :key="field.path" class="field-value-row"><code>{{ field.path }}</code><strong>{{ formatFieldValue(field.path, field.value) }}</strong><StatusBadge :value="field.present ? 'completed' : 'unresolved'" /></div></div>
          <h3 class="subheading">字段证据</h3>
          <div class="record-list"><article v-for="item in evidence" :key="item.field_path"><header><code>{{ item.field_path }}</code><span>{{ displayLabel(item.source, 'evidence_source') }} · 第 {{ item.page_number || '-' }} 页</span></header><p>{{ localizedText((item.validation_signals || []).join(' · ') || '无额外验证信号') }}</p><footer>候选 {{ item.candidate_values?.length || 0 }} 个 · 可读性 {{ displayLabel(item.readability, 'readability') }}</footer></article></div>
          <details class="technical-details" v-if="fieldBindingEvidence.length || ocrComparisons.length">
            <summary>查看字段绑定与多源技术证据</summary>
            <div class="record-list">
              <article v-for="item in fieldBindingEvidence" :key="`binding-${item.evidence_id}`">
                <header><code>{{ item.canonical_field_path || item.observed_label }}</code><StatusBadge :value="item.binding_decision?.status || 'not_reported'" /></header>
                <p>{{ fullText(item.observed_label) }} → {{ fullText(item.normalized_label) }}</p>
              </article>
              <article v-for="item in ocrComparisons" :key="`ocr-${item.canonical_field_path}`">
                <header><code>{{ item.canonical_field_path }}</code><StatusBadge :value="item.outcome || 'not_reported'" /></header>
                <p>{{ localizedText(item.reason_codes?.join(' · ') || '未报告原因') }}</p>
              </article>
            </div>
          </details>
        </template>

        <div v-if="isReview" class="review-zone">
          <div class="review-title"><ShieldCheck :size="19" /><div><strong>需要人工审核</strong><span>只有主动选择“明确确认正确”，系统才会记录为已确认正确。</span></div></div>
          <p class="trusted-actor-note"><ShieldCheck :size="16" />审核人由后端可信身份上下文确定，页面不能修改。</p>
          <details v-if="reviewEvidence.length" class="technical-details"><summary>查看当前文档的 Vision/OCR 证据</summary>
            <div class="record-list"><article v-for="item in reviewEvidence" :key="`${item.field_path}-${item.source_type}-${item.source_id}`"><header><code>{{ item.field_path }}</code><span>{{ displayLabel(item.source_type, 'evidence_source') }} · 第 {{ item.page_number || '-' }} 页</span></header><p>候选：{{ item.candidate_values?.length ? item.candidate_values.map(fullText).join('、') : '未报告' }}</p><footer>比对：{{ localizedText(item.comparison_outcome) }} · {{ localizedText(item.reason_codes?.join('、')) }}</footer></article></div>
          </details>
          <article v-for="(notice, index) in reviewNotices" :key="`document-${index}`" class="review-item"><header><strong>整份文档</strong></header><p>{{ localizedText(notice.reasons?.join('；') || '请重新提交字段级审核。') }}</p></article>
          <article
            v-for="field in reviewFields"
            :key="fieldKey(field)"
            :ref="(element) => setReviewTarget(reviewTarget('field', fieldKey(field)), element)"
            class="review-item"
            :class="{ 'review-item-error': reviewFieldErrors[reviewTarget('field', fieldKey(field))] }"
          >
            <header><code>{{ fieldKey(field) }}</code><span class="full-value">{{ formatFieldValue(fieldKey(field), field.current_value) }}</span></header>
            <p>{{ localizedText(field.reasons?.join('；') || '后端要求人工判断') }}</p>
            <div class="review-controls">
              <label :for="controlId('field', fieldKey(field), 'action')">审核决定</label>
              <select
                :id="controlId('field', fieldKey(field), 'action')"
                v-model="reviewActions[fieldKey(field)]"
                :disabled="!canSubmitReview"
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
                  :disabled="!canSubmitReview || reviewNulls[fieldKey(field)]"
                  :aria-invalid="Boolean(reviewFieldErrors[reviewTarget('field', fieldKey(field))])"
                  :aria-describedby="reviewFieldErrors[reviewTarget('field', fieldKey(field))] ? controlId('field', fieldKey(field), 'error') : undefined"
                  @input="clearReviewError(reviewTarget('field', fieldKey(field)))"
                />
                <label class="review-null-option"><input v-model="reviewNulls[fieldKey(field)]" type="checkbox" :disabled="!canSubmitReview" @change="clearReviewError(reviewTarget('field', fieldKey(field)))" />设为空</label>
              </template>
              <label :for="controlId('field', fieldKey(field), 'reason')">审核原因</label>
              <input
                :id="controlId('field', fieldKey(field), 'reason')"
                v-model="reviewReasons[fieldKey(field)]"
                :disabled="!canSubmitReview"
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
            <div class="review-controls"><select v-model="bindingSelections[binding.evidence_id]" data-review-control :disabled="!canSubmitReview" @change="clearReviewError(reviewTarget('binding', binding.evidence_id))"><option value="">选择标准字段路径</option><option v-for="path in binding.candidate_field_paths" :key="path" :value="path">{{ path }}</option></select><input v-model="bindingReasons[binding.evidence_id]" :disabled="!canSubmitReview" placeholder="字段映射确认原因" @input="clearReviewError(reviewTarget('binding', binding.evidence_id))" /></div>
            <p v-if="reviewFieldErrors[reviewTarget('binding', binding.evidence_id)]" class="review-field-error">{{ reviewFieldErrors[reviewTarget('binding', binding.evidence_id)] }}</p>
          </article>
          <div v-if="state.reviewError" class="alert error review-submit-feedback" role="alert" aria-live="assertive"><AlertCircle :size="18" /><span>{{ state.reviewError }}</span></div>
          <div v-if="state.reviewFeedback" class="alert neutral review-submit-feedback" role="status" aria-live="polite"><ShieldCheck :size="18" /><span>{{ state.reviewFeedback }}</span></div>
          <div v-if="state.readOnlyReviewReason" class="alert neutral review-submit-feedback" role="status">
            <AlertCircle :size="18" /><span>{{ state.readOnlyReviewReason }}</span>
          </div>
          <div class="button-row">
            <button
              v-if="['pending_review', 'expired'].includes(state.reviewTask?.status)"
              type="button"
              class="secondary-btn"
              :disabled="state.loading || state.reviewClaiming"
              @click="claimReviewTask"
            ><LoaderCircle v-if="state.reviewClaiming" class="spin" :size="16" /><ShieldCheck v-else :size="16" />{{ state.reviewClaiming ? '正在领取...' : '领取审核任务' }}</button>
            <button type="button" class="primary-btn grow" :disabled="state.loading || !canSubmitReview" @click="submitReview"><LoaderCircle v-if="state.reviewSubmitting" class="spin" :size="16" /><ShieldCheck v-else :size="16" />{{ state.reviewSubmitting ? '正在提交...' : '提交审核' }}</button>
          </div>
        </div>
      </section>
    </div>
    <RunFieldsDialog :open="runDialogOpen" :run-id="state.run?.run_id || ''" :prefetched-result="state.result" @close="runDialogOpen = false" />
    </template>
  </div>
</template>

<style scoped>
.extraction-mode{display:inline-flex;border:1px solid var(--line,#d8dee6);border-radius:6px;overflow:hidden;margin:12px 0 4px}.extraction-mode button{border:0;background:transparent;padding:8px 14px;cursor:pointer}.extraction-mode button[aria-pressed="true"]{background:#206b65;color:#fff}
</style>
