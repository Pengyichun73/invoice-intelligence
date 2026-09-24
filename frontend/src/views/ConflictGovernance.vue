<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { AlertTriangle, Ban, CheckCircle2, LoaderCircle, RefreshCw, ShieldCheck, X } from 'lucide-vue-next'
import PageHeader from '../components/PageHeader.vue'
import PaginationBar from '../components/PaginationBar.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { displayLabel, formatDate } from '../api/client'
import { governanceApi } from '../api/governance'
import { usePagedResource } from '../composables/usePagedResource'
import { useNotifications } from '../composables/useNotifications'

const emit = defineEmits(['navigate'])
const notices = useNotifications()

const status = ref('open')
const operationMessage = ref('')
const operationError = ref('')
const dialog = reactive({
  open: false,
  action: 'resolve',
  item: null,
  selectedPath: '',
  reason: '',
  note: '',
  busy: false,
  error: '',
})
const dialogRef = ref(null)
const reasonRef = ref(null)
let previousFocus = null
const pager = usePagedResource((cursor) => governanceApi.conflicts({ status: status.value, limit: 20, cursor }))
const isResolve = computed(() => dialog.action === 'resolve')
const requiresField = computed(() => isResolve.value && Boolean(dialog.item?.candidate_field_paths?.length))
const canSubmit = computed(() => (
  dialog.reason.trim().length > 0
  && (!requiresField.value || dialog.selectedPath.length > 0)
  && !dialog.busy
))

async function refresh() {
  await pager.load({ reset: true }).catch(() => {})
}

async function openAction(action, item) {
  operationMessage.value = ''
  operationError.value = ''
  Object.assign(dialog, {
    open: true,
    action,
    item,
    selectedPath: '',
    reason: '',
    note: '',
    busy: false,
    error: '',
  })
  previousFocus = document.activeElement
  await nextTick()
  reasonRef.value?.focus()
}

function closeDialog() {
  if (!dialog.busy) {
    dialog.open = false
    previousFocus?.focus?.()
    previousFocus = null
  }
}

function handleDialogKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    closeDialog()
    return
  }
  if (event.key !== 'Tab') return
  const controls = Array.from(dialogRef.value?.querySelectorAll('button:not([disabled]), textarea:not([disabled]), select:not([disabled])') || [])
  if (!controls.length) return
  const first = controls[0]
  const last = controls.at(-1)
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}

async function submitDecision() {
  if (!canSubmit.value || !dialog.item) return
  dialog.busy = true
  dialog.error = ''
  try {
    const result = await governanceApi.decideConflict(
      dialog.item.conflict_id,
      dialog.action,
      {
        reason: dialog.reason.trim(),
        expected_status: 'open',
        selected_canonical_field_path: isResolve.value && dialog.selectedPath ? dialog.selectedPath : null,
        resolution_note: dialog.note.trim() || null,
      },
    )
    dialog.open = false
    previousFocus?.focus?.()
    previousFocus = null
    const decisionLabel = displayLabel(result.decision.target_status, 'conflict_status')
    operationMessage.value = result.trace_id
      ? `后端已记录“${decisionLabel}”决定，技术关联标识：${result.trace_id}`
      : `后端已记录“${decisionLabel}”决定；该历史重放未采集技术关联标识`
    notices.success(`冲突已${decisionLabel}`, '治理结论已写入；仅因该冲突隔离的案例会重新入队，别名和其他案例仍需复核。', {
      actionLabel: '查看记忆准入',
      onAction: () => emit('navigate', 'admissions'),
    })
    await refresh()
    if (pager.state.error) {
      notices.warning('决定已保存，但最新状态未读取', '请只刷新冲突列表确认结果，不要重复提交解决决定。', {
        actionLabel: '重新读取',
        onAction: refresh,
      })
    }
  } catch (error) {
    if (error.status === 409) {
      dialog.open = false
      dialog.item = null
      previousFocus?.focus?.()
      previousFocus = null
      await refresh()
      if (pager.state.error) {
        notices.error('冲突已变化，最新状态读取失败', '旧状态已失效，请先刷新成功后再处理。', { duration: 0 })
      } else {
        operationError.value = error.message
        notices.warning('冲突已由其他治理人员处理', '已读取最新状态，请重新核对。')
      }
    } else {
      dialog.error = error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。'
      notices.error('冲突治理未完成', dialog.error)
    }
  } finally {
    dialog.busy = false
  }
}

onMounted(refresh)
</script>

<template>
  <div>
    <PageHeader eyebrow="冲突治理" title="字段映射冲突" description="治理结论登记重新评估；仅因该冲突隔离的案例自动重排，其他目标仍需复核。">
      <button class="secondary-btn" :disabled="pager.state.loading" @click="refresh"><RefreshCw :size="16" />刷新</button>
    </PageHeader>
    <div class="alert warning"><AlertTriangle :size="18" /><span>“解决冲突”必须从后端候选字段中选择；“忽略冲突”仅表示误报或不再适用。所有状态以刷新后的后端结果为准。</span></div>
    <div v-if="operationMessage" class="alert neutral"><CheckCircle2 :size="18" /><span>{{ operationMessage }}</span></div>
    <div v-if="operationError" class="alert error"><AlertTriangle :size="18" /><span>{{ operationError }}</span></div>
    <section class="toolbar"><label>冲突状态<select v-model="status" @change="refresh"><option value="open">待处理</option><option value="resolved">已解决</option><option value="dismissed">已忽略</option></select></label></section>
    <section class="surface">
      <ResourceState :loading="pager.state.loading" :error="pager.state.error" :empty="!pager.state.items.length" empty-text="当前没有冲突记录" @retry="refresh">
        <div class="conflict-list">
          <article v-for="item in pager.state.items" :key="item.conflict_id">
            <header><div><span class="type-label">{{ displayLabel(item.conflict_type, 'conflict_type') }}</span><h2>{{ item.field_path }}</h2></div><StatusBadge :value="item.status" /></header>
            <div class="candidate-paths"><span v-for="path in item.candidate_field_paths" :key="path">{{ path }}</span></div>
            <p>检测到 {{ item.reason_codes.length }} 项冲突依据，请结合候选字段和证据来源判断。</p>
            <dl class="meta-list"><div><dt>单据类型</dt><dd>{{ displayLabel(item.document_type, 'document_type') }}</dd></div><div><dt>数据结构版本</dt><dd>{{ item.schema_version }}</dd></div><div><dt>关联案例</dt><dd>{{ item.example_ids.length }}</dd></div><div><dt>别名候选</dt><dd>{{ item.field_alias_candidate_ids.length }}</dd></div><div><dt>证据引用</dt><dd>{{ item.evidence_references.length }} 条（敏感原值不展示）</dd></div><div><dt>检测时间</dt><dd>{{ formatDate(item.detected_at) }}</dd></div></dl>
            <div v-if="item.status === 'open'" class="button-row">
              <button class="secondary-btn danger-text" @click="openAction('dismiss', item)"><Ban :size="15" />忽略冲突</button>
              <button class="primary-btn" @click="openAction('resolve', item)"><ShieldCheck :size="15" />解决冲突</button>
            </div>
          </article>
        </div>
        <PaginationBar :count="pager.state.items.length" :can-previous="pager.state.history.length > 0" :can-next="Boolean(pager.state.nextCursor)" @previous="pager.previous" @next="pager.next" />
      </ResourceState>
    </section>

    <div v-if="dialog.open" class="dialog-backdrop" @click.self="closeDialog">
      <section ref="dialogRef" class="dialog" role="dialog" aria-modal="true" :aria-label="isResolve ? '解决冲突' : '忽略冲突'" @keydown="handleDialogKeydown">
        <header><div><AlertTriangle :size="19" /><strong>{{ isResolve ? '解决冲突' : '忽略冲突' }}</strong></div><button class="icon-btn" title="关闭" :disabled="dialog.busy" @click="closeDialog"><X :size="17" /></button></header>
        <p>此操作会写入不可变治理决策；审核人由后端可信上下文确定。</p>
        <label v-if="requiresField">确认的标准字段路径<select v-model="dialog.selectedPath" :disabled="dialog.busy"><option value="" disabled>请选择后端候选字段</option><option v-for="path in dialog.item.candidate_field_paths" :key="path" :value="path">{{ path }}</option></select></label>
        <label>操作原因<textarea ref="reasonRef" v-model="dialog.reason" maxlength="2000" rows="4" :disabled="dialog.busy" placeholder="必填：说明治理判断依据" /></label>
        <label>脱敏备注（可选）<textarea v-model="dialog.note" maxlength="2000" rows="3" :disabled="dialog.busy" placeholder="不得粘贴原始发票值、图片或 Base64" /></label>
        <div v-if="dialog.error" class="dialog-error" role="alert" aria-live="assertive">{{ dialog.error }}</div>
        <footer><button class="secondary-btn" :disabled="dialog.busy" @click="closeDialog">取消</button><button class="primary-btn" :class="{ danger: !isResolve }" :disabled="!canSubmit" @click="submitDecision"><LoaderCircle v-if="dialog.busy" class="spin" :size="16" />{{ isResolve ? '提交解决决定' : '确认忽略' }}</button></footer>
      </section>
    </div>
  </div>
</template>

<style scoped>
.dialog label + label { margin-top: 12px; }
.dialog-error { margin-top: 12px; color: #ef9198; font-size: 11px; line-height: 1.5; }
.button-row { justify-content: flex-end; margin-top: 14px; }
@media (max-width: 640px) {
  .button-row { align-items: stretch; flex-direction: column; }
  .button-row button { width: 100%; }
}
</style>
