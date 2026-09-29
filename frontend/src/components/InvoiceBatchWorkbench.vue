<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { AlertCircle, Check, FilePlus2, LoaderCircle, RefreshCw, Search, ShieldCheck, X } from 'lucide-vue-next'
import { invoiceApi } from '../api/invoice'
import { errorFeedback, formatFieldValue } from '../api/client'
import StatusBadge from './StatusBadge.vue'
import BatchReviewDialog from './BatchReviewDialog.vue'
import RunFieldsDialog from './RunFieldsDialog.vue'

const BATCH_KEY = 'invoice-intelligence-current-batch-id'
const CREATE_KEY = 'invoice-intelligence-pending-batch-key'
const files = ref([])
const batch = ref(null)
const lookupId = ref('')
const recentBatches = ref([])
const recentBatchesLoading = ref(false)
const recentBatchesError = ref('')
const loading = ref(false)
const error = ref('')
const reviewRunId = ref('')
const fieldsRunId = ref('')
const boundaryFileId = ref('')
const selectedPage = ref(1)
const drawingGroup = ref(1)
const workingGroups = ref([])
const previewUrls = reactive({})
const runStates = reactive({})
const results = reactive({})
const reviewSummaries = reactive({})
const locateEvidence = ref(null)
const canvasRef = ref(null)
const dragStart = ref(null)
let timer = null
let recentTimer = null
let disposed = false

async function loadRecentBatches() {
  recentBatchesLoading.value = true
  recentBatchesError.value = ''
  try { recentBatches.value = await invoiceApi.recentBatches() }
  catch (cause) { recentBatchesError.value = errorFeedback(cause).message }
  finally { recentBatchesLoading.value = false }
}

const fileById = computed(() => Object.fromEntries((batch.value?.files || []).map((file) => [file.file_id, file])))
const boundaryFile = computed(() => batch.value?.files?.find((file) => file.file_id === boundaryFileId.value) || null)
const items = computed(() => batch.value?.items || [])
const detectedCount = computed(() => (batch.value?.files || []).reduce((count, file) => count + (file.groups?.length || 0), 0))
const canUpload = computed(() => files.value.length > 0 && files.value.length <= 5 && !loading.value && (!batch.value || batch.value.status === 'open'))

function handleDrop(event) {
  selectFiles(event)
}

function selectFiles(event) {
  const selected = Array.from(event.target?.files || event.dataTransfer?.files || [])
  if (!selected.length) return
  if (selected.length > 5) { error.value = '一次最多选择五个文件。'; return }
  if (selected.some((file) => !['image/png', 'image/jpeg', 'image/webp', 'application/pdf'].includes(file.type))) {
    error.value = '仅支持 PNG、JPG、WEBP 和 PDF。'; return
  }
  files.value = selected
  error.value = ''
}

async function refresh() {
  if (!batch.value?.batch_id || disposed) return
  const batchId = batch.value.batch_id
  try {
    const next = await invoiceApi.batch(batchId)
    if (disposed || batch.value?.batch_id !== batchId) return
    batch.value = next
    for (const item of next.items || []) {
      if (item.regions?.[0]) void ensurePreview(item.file_id, item.regions[0].page_number)
      if (!item.run_id) continue
      try {
        const run = await invoiceApi.run(item.run_id)
        runStates[item.run_id] = run
        if (run.status === 'completed' && !results[item.run_id]) {
          results[item.run_id] = await invoiceApi.result(item.run_id)
        }
        if (run.status === 'pending_review' && !reviewSummaries[item.run_id]) {
          reviewSummaries[item.run_id] = await invoiceApi.review(item.run_id)
        }
      } catch { /* The batch and independent Run can become visible in separate transactions. */ }
    }
  } catch (cause) {
    if (!disposed && batch.value?.batch_id === batchId) error.value = errorFeedback(cause).message
  }
}

async function restoreBatch(id, silent = false) {
  const batchId = id.trim()
  if (!batchId || loading.value) return
  loading.value = true
  error.value = ''
  try {
    const restored = await invoiceApi.batch(batchId)
    if (disposed) return
    batch.value = restored
    void loadRecentBatches()
    files.value = []
    sessionStorage.setItem(BATCH_KEY, restored.batch_id)
    await refresh()
  } catch (cause) {
    if (cause.status === 404) {
      if (silent) sessionStorage.removeItem(BATCH_KEY)
      else error.value = '批次不存在或当前账号无权访问。'
    } else error.value = errorFeedback(cause).message
  } finally { loading.value = false }
}

async function start() {
  if (!canUpload.value) return
  loading.value = true
  error.value = ''
  try {
    if (!batch.value) {
      const key = sessionStorage.getItem(CREATE_KEY) || crypto.randomUUID()
      sessionStorage.setItem(CREATE_KEY, key)
      const created = await invoiceApi.createBatch(key)
      batch.value = created
      void loadRecentBatches()
      sessionStorage.setItem(BATCH_KEY, created.batch_id)
    }
    for (let index = 0; index < files.value.length; index++) {
      const file = files.value[index]
      const known = batch.value.files?.[index]
      if (known) {
        if (known.filename !== file.name) throw new Error(`第 ${index + 1} 个文件与已上传文件不一致，请重新选择原文件。`)
        continue
      }
      batch.value = await invoiceApi.addBatchFile(batch.value.batch_id, file, `${batch.value.batch_id}-upload-${index + 1}`)
    }
    batch.value = await invoiceApi.submitBatch(batch.value.batch_id, batch.value.revision, `${batch.value.batch_id}-submit`)
    files.value = []
    await refresh()
  } catch (cause) { error.value = errorFeedback(cause).message || cause.message }
  finally { loading.value = false }
}

function resetBatch() {
  if (batch.value && !['dispatched', 'extracting', 'pending_review', 'completed', 'too_many_invoices', 'failed'].includes(batch.value.status)) {
    error.value = '当前批次仍在处理；请等待或先记录批次编号。'
    return
  }
  batch.value = null
  files.value = []
  reviewRunId.value = ''
  fieldsRunId.value = ''
  sessionStorage.removeItem(BATCH_KEY)
  sessionStorage.removeItem(CREATE_KEY)
  for (const value of Object.values(previewUrls)) URL.revokeObjectURL(value)
  for (const key of Object.keys(previewUrls)) delete previewUrls[key]
}

async function ensurePreview(fileId, page) {
  const key = `${fileId}:${page}`
  if (previewUrls[key]) return previewUrls[key]
  try {
    const blob = await invoiceApi.batchPage(batch.value.batch_id, fileId, page)
    if (disposed) return ''
    previewUrls[key] = URL.createObjectURL(blob)
    return previewUrls[key]
  } catch (cause) { error.value = errorFeedback(cause).message; return '' }
}

function openBoundary(file) {
  boundaryFileId.value = file.file_id
  selectedPage.value = 1
  drawingGroup.value = 1
  workingGroups.value = structuredClone(file.groups || [])
  locateEvidence.value = null
  void ensurePreview(file.file_id, 1)
}

function openLocation(payload) {
  const item = items.value.find((entry) => entry.run_id === reviewRunId.value)
  const file = fileById.value[item?.file_id]
  if (!file) return
  openBoundary(file)
  selectedPage.value = payload.region?.page_number || 1
  locateEvidence.value = payload
  void ensurePreview(file.file_id, selectedPage.value)
}

function pageRegions() {
  return workingGroups.value.flatMap((group, groupIndex) => (group.regions || [])
    .filter((region) => region.page_number === selectedPage.value)
    .map((region) => ({ ...region, groupIndex })))
}

function point(event) {
  const rect = canvasRef.value?.getBoundingClientRect()
  if (!rect) return null
  return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
           y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) }
}

function beginRegion(event) { dragStart.value = point(event) }
function endRegion(event) {
  const end = point(event)
  const start = dragStart.value
  dragStart.value = null
  if (!start || !end) return
  const left = Math.min(start.x, end.x)
  const top = Math.min(start.y, end.y)
  const right = Math.max(start.x, end.x)
  const bottom = Math.max(start.y, end.y)
  if (right - left < 0.02 || bottom - top < 0.02) return
  const groupIndex = Math.max(0, Math.min(4, Number(drawingGroup.value) - 1))
  while (workingGroups.value.length <= groupIndex) workingGroups.value.push({ regions: [] })
  workingGroups.value[groupIndex].regions.push({ page_number: selectedPage.value, left, top, right, bottom })
}

async function confirmBoundary() {
  if (!boundaryFile.value || loading.value) return
  loading.value = true
  error.value = ''
  try {
    const groups = workingGroups.value.filter((group) => group.regions.length)
    batch.value = await invoiceApi.confirmBatchFile(batch.value.batch_id, boundaryFileId.value,
      boundaryFile.value.revision, groups)
    boundaryFileId.value = ''
  } catch (cause) { error.value = errorFeedback(cause).message }
  finally { loading.value = false }
}

async function retryItem(item) {
  if (!item.run_id || loading.value) return
  loading.value = true
  error.value = ''
  try {
    batch.value = await invoiceApi.retryBatchItem(batch.value.batch_id, item.item_id, item.run_id)
    delete runStates[item.run_id]
    await refresh()
  } catch (cause) { error.value = errorFeedback(cause).message }
  finally { loading.value = false }
}

function summary(item) {
  const invoice = results[item.run_id]?.result?.invoice || reviewSummaries[item.run_id]?.current_invoice
  if (!invoice) return null
  return {
    number: invoice.invoice_number || invoice.invoice_unique_code,
    date: invoice.invoice_date,
    seller: invoice.seller_name,
    amount: invoice.invoice_total_amount,
  }
}

onMounted(async () => {
  void loadRecentBatches()
  const saved = sessionStorage.getItem(BATCH_KEY)
  if (saved) await restoreBatch(saved, true)
  if (!disposed) timer = setInterval(() => { if (batch.value && !disposed) void refresh() }, 3000)
  if (!disposed) recentTimer = setInterval(() => { if (!disposed && !recentBatchesLoading.value) void loadRecentBatches() }, 10000)
})
onBeforeUnmount(() => {
  disposed = true
  clearInterval(timer)
  clearInterval(recentTimer)
  for (const value of Object.values(previewUrls)) URL.revokeObjectURL(value)
})
</script>

<template>
  <section class="batch-workbench" aria-label="多发票提取">
    <header class="batch-head"><div><span>批次提取</span><h2>多张发票</h2></div><div class="button-row"><button v-if="batch" type="button" class="icon-btn" title="刷新批次" @click="refresh"><RefreshCw :size="17" /></button><button v-if="batch" type="button" class="secondary-btn" @click="resetBatch"><FilePlus2 :size="16" />新建批次</button></div></header>
    <p v-if="error" class="alert error" role="alert"><AlertCircle :size="16" />{{ error }}</p>
    <form v-if="!batch" class="batch-lookup" @submit.prevent="restoreBatch(lookupId)"><label for="batch-lookup-id">查询已有批次</label><input id="batch-lookup-id" v-model.trim="lookupId" type="text" maxlength="100" autocomplete="off" placeholder="输入 batch_id" /><button class="secondary-btn" type="submit" :disabled="!lookupId.trim() || loading"><Search :size="16" />查询批次</button></form>
    <div class="recent-ids"><div class="recent-ids-head"><strong>我的最近批次</strong><button class="icon-btn" type="button" title="刷新最近批次" :disabled="recentBatchesLoading" @click="loadRecentBatches"><RefreshCw :size="15" /></button></div><p v-if="recentBatchesError" role="alert">{{ recentBatchesError }}</p><p v-else-if="recentBatchesLoading && !recentBatches.length">正在加载...</p><p v-else-if="!recentBatches.length">暂无可查询的批次</p><ul v-else><li v-for="item in recentBatches" :key="item.batch_id"><button type="button" :disabled="loading" @click="restoreBatch(item.batch_id)"><code>{{ item.batch_id }}</code><small>{{ new Date(item.created_at).toLocaleString('zh-CN') }}</small><span v-if="item.status === 'open'" class="status-badge status-open">待上传</span><StatusBadge v-else :value="item.status" /></button></li></ul></div>
    <div v-if="!batch || batch.status === 'open'" class="batch-upload" @dragover.prevent @drop.prevent="handleDrop"><input id="batch-files" type="file" multiple accept="image/png,image/jpeg,image/webp,application/pdf" @change="selectFiles" /><label for="batch-files">选择或拖入最多五个文件</label><span>{{ files.length ? files.map((file) => file.name).join('、') : 'PNG、JPG、WEBP 或 PDF' }}</span><button class="primary-btn" type="button" :disabled="!canUpload" @click="start"><LoaderCircle v-if="loading" class="spin" :size="16" /><FilePlus2 v-else :size="16" />{{ loading ? '正在登记' : '开始批次提取' }}</button></div>
    <div v-if="batch" class="batch-overview"><code>{{ batch.batch_id }}</code><span v-if="batch.status === 'open'" class="status-badge status-open">待上传</span><StatusBadge v-else :value="batch.status" /><span>源文件 {{ batch.files.length }}/5 · 已识别票据 {{ detectedCount }}/5</span></div>
    <p v-if="batch?.status === 'too_many_invoices'" class="alert error" role="alert">检查到当前发票数据超过五张，本批次没有启动提取。请拆成更小的批次重新上传。</p>
    <p v-if="batch?.status === 'failed'" class="alert error" role="alert">{{ items.length ? '部分票据提取失败；请查看对应 Run 并逐票重试。' : '票据边界识别失败；已登记的文件和批次编号仍保留，请查看错误码或重新建立批次。' }}</p>
    <div v-if="batch?.files?.length" class="batch-source-list"><div v-for="file in batch.files" :key="file.file_id" class="batch-source"><strong>文件 {{ file.ordinal }} · {{ file.filename || '未命名文件' }}</strong><StatusBadge :value="file.status" /><span>{{ file.page_count ? `${file.page_count} 页` : '等待页数' }} · {{ file.groups?.length || 0 }} 张票据</span><button v-if="file.status === 'needs_review'" class="secondary-btn" type="button" @click="openBoundary(file)"><ShieldCheck :size="15" />确认票据边界</button><span v-if="file.error_code">{{ file.error_code }}</span></div></div>
    <div v-if="items.length" class="batch-item-grid"><article v-for="item in items" :key="item.item_id" class="batch-invoice-card"><header><strong>第 {{ item.ordinal }} 张发票</strong><StatusBadge :value="runStates[item.run_id]?.status || 'received'" /></header><img v-if="previewUrls[`${item.file_id}:${item.regions[0]?.page_number}`]" class="batch-card-thumb" :src="previewUrls[`${item.file_id}:${item.regions[0]?.page_number}`]" alt="发票来源页缩略图" /><p>源文件 {{ fileById[item.file_id]?.ordinal || '-' }} · 原件第 {{ item.regions.map((region) => region.page_number).join('、') }} 页</p><code v-if="item.run_id">{{ item.run_id }}</code><p v-else>正在建立独立提取任务...</p><dl v-if="summary(item)"><div><dt>发票号码</dt><dd>{{ summary(item).number || '未识别' }}</dd></div><div><dt>开票日期</dt><dd>{{ formatFieldValue('invoice_date', summary(item).date) }}</dd></div><div><dt>销方</dt><dd>{{ summary(item).seller || '未识别' }}</dd></div><div><dt>金额</dt><dd>{{ summary(item).amount ?? '未识别' }}</dd></div></dl><footer><button v-if="results[item.run_id]" type="button" class="secondary-btn" @click="fieldsRunId = item.run_id">查看全部字段</button><button v-if="runStates[item.run_id]?.status === 'failed'" type="button" class="secondary-btn" :disabled="loading" @click="retryItem(item)"><RefreshCw :size="15" />重试此票</button><button v-if="runStates[item.run_id]?.status === 'pending_review'" type="button" class="primary-btn" @click="reviewRunId = item.run_id"><ShieldCheck :size="15" />人工审核</button><button v-if="item.regions.length" type="button" class="secondary-btn" @click="openBoundary(fileById[item.file_id])">查看来源</button></footer></article></div>
    <div v-if="boundaryFile" class="dialog-backdrop" @click.self="boundaryFileId = ''"><section class="dialog batch-boundary-dialog" role="dialog" aria-modal="true" aria-label="发票边界核对"><header><div><strong>{{ locateEvidence ? '字段来源位置' : '确认发票边界' }}</strong><span>文件 {{ boundaryFile.ordinal }} · 第 {{ selectedPage }} 页</span></div><button class="icon-btn" title="关闭" @click="boundaryFileId = ''"><X :size="17" /></button></header><div class="batch-page-tabs"><button v-for="page in boundaryFile.page_count || 1" :key="page" type="button" :class="{ active: selectedPage === page }" @click="selectedPage = page; ensurePreview(boundaryFile.file_id, page)">第 {{ page }} 页</button></div><p v-if="locateEvidence" class="context-note">字段 {{ locateEvidence.evidence.field_path }} · {{ locateEvidence.originalBox ? '青色框标明字段在原件中的位置。' : '无法精确定位字段；红色框仅标明票据区域。' }} 请以原件内容判断是否串值。</p><div ref="canvasRef" class="batch-page-canvas" @pointerdown="!locateEvidence && beginRegion($event)" @pointerup="!locateEvidence && endRegion($event)"><img v-if="previewUrls[`${boundaryFile.file_id}:${selectedPage}`]" :src="previewUrls[`${boundaryFile.file_id}:${selectedPage}`]" alt="当前源文件页面" draggable="false" /><div v-if="locateEvidence?.originalBox" class="batch-evidence-box" :style="{ left: `${locateEvidence.originalBox.left * 100}%`, top: `${locateEvidence.originalBox.top * 100}%`, width: `${(locateEvidence.originalBox.right - locateEvidence.originalBox.left) * 100}%`, height: `${(locateEvidence.originalBox.bottom - locateEvidence.originalBox.top) * 100}%` }"></div><div v-for="(region, index) in pageRegions()" :key="index" class="batch-region" :style="{ left: `${region.left * 100}%`, top: `${region.top * 100}%`, width: `${(region.right - region.left) * 100}%`, height: `${(region.bottom - region.top) * 100}%` }"><span>票据 {{ region.groupIndex + 1 }}</span></div></div><template v-if="!locateEvidence && boundaryFile.status === 'needs_review'"><div class="batch-boundary-controls"><label>绘制到票据组 <input v-model.number="drawingGroup" type="number" min="1" max="5" /></label><span>在图片上拖动添加区域；相同组可跨页。</span></div><div v-for="(group, groupIndex) in workingGroups" :key="groupIndex" class="batch-group-row"><strong>票据 {{ groupIndex + 1 }}</strong><span v-for="(region, regionIndex) in group.regions" :key="regionIndex">第 {{ region.page_number }} 页 <button class="icon-btn" title="移除区域" @click="group.regions.splice(regionIndex, 1)"><X :size="14" /></button></span></div><button class="primary-btn" type="button" :disabled="loading" @click="confirmBoundary"><Check :size="16" />确认边界并继续</button></template></section></div>
    <BatchReviewDialog :open="Boolean(reviewRunId)" :run-id="reviewRunId" :regions="items.find((item) => item.run_id === reviewRunId)?.regions || []" :derived-pages="items.find((item) => item.run_id === reviewRunId)?.derived_pages || []" @close="reviewRunId = ''" @updated="refresh" @locate="openLocation" />
    <RunFieldsDialog :open="Boolean(fieldsRunId)" :run-id="fieldsRunId" :prefetched-result="results[fieldsRunId] || null" @close="fieldsRunId = ''" />
  </section>
</template>

<style scoped>
.batch-workbench .dialog-backdrop{z-index:101}
.batch-lookup{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 0;border-top:1px solid var(--line,#d8dee6)}.batch-lookup label{font-size:14px;font-weight:600}.batch-lookup input{min-width:min(100%,260px);max-width:100%;flex:1}
.batch-card-thumb{width:100%;height:96px;object-fit:contain;background:#f3f6f6;margin-top:8px;border:1px solid #d8dee6}
.batch-evidence-box{position:absolute;border:3px solid #00a6a0;background:rgba(0,166,160,.16);z-index:2;pointer-events:none}
.batch-workbench{padding:18px 0 30px}.batch-head,.batch-overview,.batch-source,.batch-invoice-card header,.batch-invoice-card footer,.batch-boundary-dialog header{display:flex;align-items:center;justify-content:space-between;gap:12px}.batch-head h2{font-size:19px;margin:3px 0}.batch-head span{font-size:14px;color:var(--text-muted,#667085)}.batch-upload{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:18px 0;border-top:1px solid var(--line,#d8dee6)}.batch-upload input{max-width:260px}.batch-upload span{min-width:160px;flex:1;overflow-wrap:anywhere}.batch-overview{justify-content:flex-start;border-top:1px solid var(--line,#d8dee6);padding:12px 0;flex-wrap:wrap}.batch-overview code,.batch-invoice-card code{overflow-wrap:anywhere}.batch-source-list{border-top:1px solid var(--line,#d8dee6)}.batch-source{padding:10px 0;border-bottom:1px solid var(--line,#d8dee6);flex-wrap:wrap}.batch-source strong{min-width:180px;overflow-wrap:anywhere}.batch-item-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,280px),1fr));gap:12px;padding:18px 0}.batch-invoice-card{border:1px solid var(--line,#d8dee6);border-radius:6px;padding:14px;min-width:0}.batch-invoice-card p{font-size:13px;color:var(--text-muted,#667085)}.batch-invoice-card dl{display:grid;gap:6px;margin:12px 0}.batch-invoice-card dl div{display:grid;grid-template-columns:80px minmax(0,1fr);gap:8px}.batch-invoice-card dt{color:var(--text-muted,#667085)}.batch-invoice-card dd{margin:0;overflow-wrap:anywhere}.batch-invoice-card footer{justify-content:flex-start;margin-top:12px;flex-wrap:wrap}.batch-boundary-dialog{width:min(940px,96vw);max-height:92vh;overflow:auto}.batch-page-tabs{display:flex;gap:6px;overflow:auto;padding:10px 0}.batch-page-tabs button{white-space:nowrap}.batch-page-tabs button.active{font-weight:700;border-bottom:2px solid currentColor}.batch-page-canvas{position:relative;display:inline-block;max-width:100%;user-select:none;touch-action:none}.batch-page-canvas img{display:block;max-width:100%;max-height:60vh}.batch-region{position:absolute;border:2px solid #e5484d;background:rgba(229,72,77,.08);pointer-events:none}.batch-region span{background:#e5484d;color:#fff;font-size:14px;padding:2px 4px}.batch-boundary-controls,.batch-group-row{display:flex;align-items:center;gap:12px;padding:8px 0;flex-wrap:wrap}.batch-boundary-controls input{width:60px}
</style>
