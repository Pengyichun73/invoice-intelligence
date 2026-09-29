<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { AlertCircle, LoaderCircle, ShieldCheck, X } from 'lucide-vue-next'
import { invoiceApi } from '../api/invoice'
import { errorFeedback, formatFieldValue, fullText } from '../api/client'
import { buildReviewSubmission, buildReviewSubmissionEnvelope, reviewInputMetadata } from '../views/extractionReview'

const props = defineProps({ open: Boolean, runId: { type: String, default: '' }, regions: { type: Array, default: () => [] }, derivedPages: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'updated', 'locate'])
const task = ref(null)
const run = ref(null)
const leaseToken = ref(null)
const busy = ref(false)
const error = ref('')
const feedback = ref('')
const actions = reactive({})
const values = reactive({})
const reasons = reactive({})
const nulls = reactive({})
const bindingSelections = reactive({})
const bindingReasons = reactive({})
const fields = computed(() => task.value?.request?.fields?.filter((item) => item.field_path) || [])
const bindings = computed(() => task.value?.request?.field_bindings || [])
const evidence = computed(() => task.value?.request?.evidence_sources || [])
const canSubmit = computed(() => task.value?.status === 'claimed' && !!leaseToken.value && !busy.value)

function clearInputs() {
  for (const map of [actions, values, reasons, nulls, bindingSelections, bindingReasons]) {
    for (const key of Object.keys(map)) delete map[key]
  }
  for (const field of fields.value) actions[field.field_path] = 'confirm_correct'
}

async function refresh() {
  if (!props.runId) return
  busy.value = true
  error.value = ''
  leaseToken.value = null
  try {
    run.value = await invoiceApi.run(props.runId)
    if (run.value.status === 'pending_review') {
      task.value = await invoiceApi.review(props.runId)
      clearInputs()
    } else task.value = null
  } catch (cause) { error.value = errorFeedback(cause).message }
  finally { busy.value = false }
}

watch(() => [props.open, props.runId], ([open]) => { if (open) void refresh() }, { immediate: true })

async function claim() {
  if (!task.value || !['pending_review', 'expired'].includes(task.value.status)) return
  busy.value = true
  error.value = ''
  try {
    const response = await invoiceApi.claimReview(task.value.review_id, task.value.revision)
    task.value = response.task
    leaseToken.value = response.lease_token || null
    clearInputs()
    if (!leaseToken.value) error.value = '后端未返回审核租约，不能提交。'
  } catch (cause) { error.value = errorFeedback(cause).message; await refresh() }
  finally { busy.value = false }
}

async function submit() {
  if (!canSubmit.value) return
  error.value = ''
  feedback.value = ''
  let correction
  try {
    correction = buildReviewSubmission({
      currentInvoice: task.value.current_invoice,
      reviewFields: fields.value,
      reviewBindings: bindings.value,
      reviewActions: actions,
      reviewValues: values,
      reviewReasons: reasons,
      bindingSelections,
      bindingReasons,
      reviewNulls: nulls,
    })
  } catch (cause) { error.value = cause.message; return }
  busy.value = true
  try {
    const response = await invoiceApi.submitClaimedReview(
      task.value.review_id,
      buildReviewSubmissionEnvelope({
        expectedRevision: task.value.revision,
        leaseToken: leaseToken.value,
        correction,
      }),
    )
    task.value = response.task
    run.value = response.run
    leaseToken.value = null
    feedback.value = '审核决定已保存，后端正在继续处理。'
    emit('updated', props.runId)
  } catch (cause) {
    error.value = errorFeedback(cause).message
    if (cause.status === 409) await refresh()
  } finally { busy.value = false }
}

function locate(item) {
  const index = (item.page_number || 1) - 1
  const region = props.regions[index]
  const dimensions = props.derivedPages[index]
  let originalBox = null
  if (region && dimensions?.width && dimensions?.height && item.bounding_box
      && item.bounding_box[0] >= 0 && item.bounding_box[1] >= 0
      && item.bounding_box[2] <= dimensions.width
      && item.bounding_box[3] <= dimensions.height) {
    const [left, top, right, bottom] = item.bounding_box
    originalBox = {
      left: region.left + left / dimensions.width * (region.right - region.left),
      top: region.top + top / dimensions.height * (region.bottom - region.top),
      right: region.left + right / dimensions.width * (region.right - region.left),
      bottom: region.top + bottom / dimensions.height * (region.bottom - region.top),
    }
  }
  emit('locate', { evidence: item, region, originalBox })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dialog-backdrop" @click.self="emit('close')">
      <section class="dialog batch-review-dialog" role="dialog" aria-modal="true" aria-label="发票人工审核" @keydown.esc="emit('close')">
        <header><div><ShieldCheck :size="18" /><strong>发票人工审核</strong></div><button class="icon-btn" type="button" title="关闭" @click="emit('close')"><X :size="17" /></button></header>
        <p class="run-fields-id">运行编号：<code>{{ runId }}</code></p>
        <p v-if="busy" role="status"><LoaderCircle class="spin" :size="16" />正在读取...</p>
        <p v-if="error" class="alert error" role="alert"><AlertCircle :size="16" />{{ error }}</p>
        <p v-if="feedback" class="alert neutral" role="status">{{ feedback }}</p>
        <p v-if="task?.status === 'claimed' && !leaseToken" class="context-note">审核任务已被领取；当前窗口没有可用租约，只能查看。</p>
        <div v-if="task" class="table-wrap batch-review-table">
          <table><thead><tr><th>字段</th><th>当前值</th><th>原因及证据</th><th>审核决定</th><th>修正值</th><th>原因</th></tr></thead>
            <tbody><tr v-for="field in fields" :key="field.field_path">
              <td><code>{{ field.field_path }}</code></td>
              <td>{{ formatFieldValue(field.field_path, field.current_value) }}</td>
              <td><span>{{ field.reasons?.join('；') || '请核对原件' }}</span><div v-for="item in evidence.filter((entry) => entry.field_path === field.field_path)" :key="`${item.source_id}-${item.page_number}`" class="batch-evidence-line"><button class="link-button" type="button" @click="locate(item)">第 {{ item.page_number || '-' }} 页定位</button><span>{{ item.bounding_box ? `位置 ${item.bounding_box.join(', ')}` : '无法定位' }} · {{ item.candidate_values?.map(fullText).join('、') || '无候选值' }}</span></div></td>
              <td><select v-model="actions[field.field_path]" :disabled="!canSubmit"><option value="confirm_correct">确认正确</option><option value="correct">人工修正</option><option value="confirm_incorrect">确认错误</option></select></td>
              <td><template v-if="actions[field.field_path] === 'correct'"><input v-model="values[field.field_path]" :type="reviewInputMetadata(field.field_path).type" :disabled="!canSubmit || nulls[field.field_path]" /><label><input v-model="nulls[field.field_path]" type="checkbox" :disabled="!canSubmit" />设为空</label></template></td>
              <td><input v-model="reasons[field.field_path]" :disabled="!canSubmit" placeholder="修正或否定时必填" /></td>
            </tr></tbody>
          </table>
        </div>
        <div v-if="bindings.length" class="batch-binding-review"><h3>字段映射确认</h3><div v-for="binding in bindings" :key="binding.evidence_id" class="batch-binding-row"><span>{{ binding.observed_label }}</span><select v-model="bindingSelections[binding.evidence_id]" :disabled="!canSubmit"><option value="">选择字段</option><option v-for="path in binding.candidate_field_paths" :key="path" :value="path">{{ path }}</option></select><input v-model="bindingReasons[binding.evidence_id]" :disabled="!canSubmit" placeholder="确认原因" /></div></div>
        <footer class="button-row"><button v-if="task && ['pending_review', 'expired'].includes(task.status)" class="secondary-btn" :disabled="busy" @click="claim">领取审核</button><button class="primary-btn" :disabled="!canSubmit" @click="submit">提交该票审核</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.batch-review-dialog{width:min(1180px,96vw);max-height:92vh;overflow:auto}.batch-review-table{max-height:55vh;overflow:auto}.batch-review-table table{min-width:850px}.batch-review-table td{vertical-align:top;max-width:270px;overflow-wrap:anywhere}.batch-review-table input:not([type="checkbox"]),.batch-review-table select{max-width:170px}.batch-evidence-line{display:flex;gap:5px;flex-wrap:wrap;padding-top:4px;font-size:14px}.batch-binding-review{padding:12px 0}.batch-binding-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:5px 0}.batch-review-dialog footer{position:sticky;bottom:0;background:inherit;padding:12px 0}
</style>
