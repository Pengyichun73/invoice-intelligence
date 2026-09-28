<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { List, X } from 'lucide-vue-next'
import { formatFieldValue } from '../api/client'
import { governanceApi } from '../api/governance'
import { invoiceApi } from '../api/invoice'
import StatusBadge from './StatusBadge.vue'

const props = defineProps({
  open: Boolean,
  runId: { type: String, default: '' },
  prefetchedResult: { type: Object, default: null },
})
const emit = defineEmits(['close'])
const result = ref(null)
const admissions = ref([])
const loading = ref(false)
const error = ref('')
const admissionError = ref('')
const dialogRef = ref(null)
let previousFocus = null
const fields = computed(() => Object.entries(result.value?.result?.invoice || {}).map(([path, value]) => ({ path, value })))
const admissionsByField = computed(() => admissions.value.reduce((byField, item) => {
  if (!byField[item.field_path]) byField[item.field_path] = []
  byField[item.field_path].push(item)
  return byField
}, {}))
const approvedCases = (fieldPath) => (admissionsByField.value[fieldPath] || []).filter(
  (item) => item.status === 'approved' && item.is_reviewed && item.is_valid,
)

watch(() => [props.open, props.runId, props.prefetchedResult], async (_values, _oldValues, onCleanup) => {
  let cancelled = false
  onCleanup(() => { cancelled = true })
  if (!props.open || !props.runId) {
    await nextTick()
    previousFocus?.focus?.()
    previousFocus = null
    return
  }
  if (!previousFocus) previousFocus = document.activeElement
  await nextTick()
  if (cancelled) return
  dialogRef.value?.querySelector('button')?.focus()
  loading.value = true
  error.value = ''
  admissionError.value = ''
  result.value = props.prefetchedResult?.run_id === props.runId ? props.prefetchedResult : null
  admissions.value = []
  try {
    const envelope = result.value || await invoiceApi.result(props.runId)
    if (cancelled) return
    result.value = envelope
    let cursor = null
    const records = []
    do {
      const page = await governanceApi.admissions({ run_id: props.runId, limit: 100, cursor })
      if (cancelled) return
      records.push(...page.items)
      cursor = page.next_cursor
    } while (cursor && records.length < 500)
    admissions.value = records
    if (cursor) admissionError.value = '准入记录超过 500 条，仅展示已读取部分。'
  } catch (requestError) {
    if (cancelled) return
    if (result.value) admissionError.value = '准入状态暂不可读取；提取字段仍可查看。'
    else error.value = requestError.message
  } finally {
    if (!cancelled) loading.value = false
  }
}, { immediate: true })

function close() { emit('close') }
function handleKeydown(event) {
  if (event.key === 'Escape') { event.preventDefault(); close() }
  if (event.key !== 'Tab') return
  const controls = Array.from(dialogRef.value?.querySelectorAll('button:not([disabled])') || [])
  if (!controls.length) return
  if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1).focus() }
  else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0].focus() }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog-backdrop" @click.self="close">
      <section ref="dialogRef" class="dialog run-fields-dialog" role="dialog" aria-modal="true" aria-label="当前运行字段总览" @keydown="handleKeydown">
        <header><div><List :size="18" /><strong>当前运行字段总览</strong></div><button class="icon-btn" title="关闭" aria-label="关闭" @click="close"><X :size="17" /></button></header>
        <p class="run-fields-id">运行编号：<code>{{ runId }}</code></p>
        <p v-if="loading" role="status">正在读取当前运行数据...</p>
        <p v-if="error" class="dialog-error" role="alert">{{ error }}</p>
        <p v-if="admissionError" class="context-note">{{ admissionError }}</p>
        <div v-if="result" class="run-fields-list">
          <div v-for="field in fields" :key="field.path" class="run-fields-row">
            <code>{{ field.path }}</code>
            <strong>{{ formatFieldValue(field.path, field.value) }}</strong>
            <span v-if="approvedCases(field.path).length">已准入案例 {{ approvedCases(field.path).length }}</span>
            <span v-else-if="admissionsByField[field.path]?.length">审核候选 <StatusBadge :value="admissionsByField[field.path][0].status" /></span>
            <span v-else>{{ loading ? '准入读取中' : admissionError ? '准入未查询' : '仅提取结果' }}</span>
            <div v-if="approvedCases(field.path).length" class="run-case-values"><div v-for="item in approvedCases(field.path)" :key="item.admission_id">已审核案例值：{{ formatFieldValue(field.path, item.reviewed_value) }}</div></div>
          </div>
          <p v-if="!fields.length" class="empty-row">当前运行未返回结构化字段</p>
        </div>
        <footer><button class="secondary-btn" @click="close">关闭</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.run-fields-dialog { width: min(900px, 100%); max-height: min(88vh, 900px); display: flex; flex-direction: column; }
.run-fields-id { overflow-wrap: anywhere; }
.run-fields-list { overflow: auto; min-height: 0; border-top: 1px solid #31454d; }
.run-fields-row { display: grid; grid-template-columns: minmax(130px, .8fr) minmax(0, 1.4fr) 120px; gap: 12px; align-items: start; padding: 10px 4px; border-bottom: 1px solid #253442; }
.run-fields-row strong { overflow-wrap: anywhere; white-space: pre-wrap; font-size: 11px; font-weight: 500; }
.run-fields-row > span { text-align: right; font-size: 11px; }
.run-case-values { grid-column: 2 / -1; color: #8ebfc0; font-size: 11px; overflow-wrap: anywhere; }
.dialog-error { color: #ef9198; }
@media (max-width: 640px) { .run-fields-row { grid-template-columns: minmax(0, 1fr) auto; } .run-fields-row strong { grid-column: 1 / -1; grid-row: 2; } .run-case-values { grid-column: 1 / -1; } }
</style>
