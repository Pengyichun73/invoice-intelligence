<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { List, RefreshCw, RotateCcw, Search, ShieldAlert, ShieldCheck, X, XCircle } from 'lucide-vue-next'
import ActionDialog from '../components/ActionDialog.vue'
import RunFieldsDialog from '../components/RunFieldsDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import PaginationBar from '../components/PaginationBar.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError, displayLabel, errorFeedback, formatDate, formatFieldValue, formatScore, fullText, localizedText } from '../api/client'
import { governanceApi } from '../api/governance'
import { usePagedResource } from '../composables/usePagedResource'
import { useNotifications } from '../composables/useNotifications'

const emit = defineEmits(['navigate'])
const notices = useNotifications()

const status = ref('all')
const runIdDraft = ref('')
const runIdFilter = ref('')
const fieldPathDraft = ref('')
const fieldPathFilter = ref('')
const detail = ref(null)
const detailError = ref('')
const selectedDetailId = ref('')
const runDialogId = ref('')
const detailLoading = ref(false)
const operationMessage = ref('')
const operationError = ref('')
const selectedIds = ref(new Set())
let detailRequestId = 0
const action = reactive({ open: false, type: '', item: null, batch: false, busy: false })
const pager = usePagedResource(async (cursor) => {
  const requestedRunId = runIdFilter.value
  const page = await governanceApi.admissions({
    ...(status.value === 'all' ? {} : { status: status.value }),
    ...(requestedRunId ? { run_id: requestedRunId } : {}),
    ...(fieldPathFilter.value ? { field_path: fieldPathFilter.value } : {}),
    limit: 20,
    cursor,
  })
  return page
})
const listError = computed(() => pager.state.error
  ? `准入列表读取失败：${pager.state.error}。当前无法确认记录状态，读取失败不代表记录已被拒绝。`
  : '')
const selectedItems = computed(() => pager.state.items.filter((item) => selectedIds.value.has(item.admission_id)))
const selectableItems = computed(() => pager.state.error || pager.state.loading
  ? []
  : pager.state.items.filter((item) => ['pending', 'quarantined'].includes(item.status)))
const allPageSelected = computed(() => selectableItems.value.length > 0 && selectableItems.value.every((item) => selectedIds.value.has(item.admission_id)))
const latestDeterministicAssessment = computed(() => detail.value?.assessments?.filter(
  (item) => item.source === 'deterministic' && item.policy_version === detail.value.policy_version,
).at(-1))
const failedSignals = computed(() => latestDeterministicAssessment.value?.signals?.filter(
  (signal) => signal.verdict === 'failed',
) || [])

async function refresh(detailId = null) {
  selectedIds.value = new Set()
  detailRequestId += 1
  detail.value = null
  detailError.value = ''
  detailLoading.value = false
  selectedDetailId.value = detailId || ''
  await pager.load({ reset: true }).catch(() => {})
  if (detailId) await loadDetail(detailId)
}
function applyRunFilter() {
  operationMessage.value = ''
  operationError.value = ''
  runIdFilter.value = runIdDraft.value.trim()
  fieldPathFilter.value = fieldPathDraft.value.trim()
  refresh()
}
function clearRunFilter() {
  runIdDraft.value = ''
  fieldPathDraft.value = ''
  applyRunFilter()
}
const ADMISSION_FAILURES = Object.freeze({
  'A current deterministic quality assessment is required before approval': '缺少当前确定性质量评估',
  'Deterministic quality failures cannot be overridden by human approval': '确定性质量检查未通过',
  'Open memory conflicts must be resolved before approval': '存在未解决的记忆冲突',
  'Memory admission revision is stale': '修订号已变化',
  'Memory admission already has the requested status': '记录已处于目标状态',
})
function failureLabel(item) {
  if (item instanceof ApiError) {
    if (item.status === 503 || item.status === 0) {
      return `服务端暂不可用，操作结果尚未确认；请先重新读取状态，勿重复提交。${item.traceId ? ` 技术关联标识：${item.traceId}` : ''}`
    }
    return errorFeedback(item).message
  }
  const message = item?.error_message || item?.payload?.message
  return Object.hasOwn(ADMISSION_FAILURES, message)
    ? ADMISSION_FAILURES[message]
    : '未处理，请核对当前状态'
}
async function loadDetail(admissionId = selectedDetailId.value) {
  if (!admissionId) return
  const requestId = ++detailRequestId
  selectedDetailId.value = admissionId
  detail.value = null
  detailError.value = ''
  detailLoading.value = true
  try {
    const current = await governanceApi.admission(admissionId)
    if (requestId === detailRequestId) detail.value = current
  } catch (error) {
    if (requestId === detailRequestId) {
      detailError.value = error instanceof ApiError && (error.status === 503 || error.status === 0)
        ? `详情暂时无法读取，当前状态未确认；不能据此判断记录已被拒绝。${error.traceId ? ` 技术关联标识：${error.traceId}` : ''}`
        : errorFeedback(error).message
    }
  } finally {
    if (requestId === detailRequestId) detailLoading.value = false
  }
}
function select(item) { return loadDetail(item.admission_id) }
function toggleSelection(item, checked) {
  const next = new Set(selectedIds.value)
  if (checked) next.add(item.admission_id)
  else next.delete(item.admission_id)
  selectedIds.value = next
}
function togglePage(checked) {
  selectedIds.value = checked
    ? new Set(selectableItems.value.map((item) => item.admission_id))
    : new Set()
}
function actionAllowed(type) {
  if (pager.state.error || pager.state.loading) return false
  const allowed = {
    approve: ['pending', 'quarantined'],
    reassess: ['quarantined'],
    quarantine: ['pending'],
    reject: ['pending', 'quarantined'],
  }
  return selectedItems.value.length > 0 && selectedItems.value.every((item) => allowed[type].includes(item.status))
}
function actionLabel(type) { return ({ approve: '批准', reassess: '重新评估', quarantine: '隔离', reject: '拒绝' })[type] || type }
function signalLabel(signal) {
  if (signal.code === 'evidence.image_resolution') return signal.verdict === 'failed' ? '图片分辨率不足' : '图片分辨率达标'
  if (signal.code === 'evidence.image_clarity') return signal.verdict === 'failed' ? '图片清晰度不足' : '图片清晰度达标'
  if (signal.code?.startsWith('ownership.')) return signal.verdict === 'failed' ? '来源归属校验失败' : '来源归属校验通过'
  if (signal.code?.startsWith('support.')) return '案例来源多样性'
  if (signal.code?.startsWith('business_rule.')) return '业务规则校验'
  if (signal.code?.startsWith('format.')) return '字段格式校验'
  return ({
    'schema.version_current': '数据结构版本有效',
    'schema.version_inactive': '数据结构版本已失效',
    'schema.document_type_exists': '单据类型有效',
    'schema.document_type_missing': '单据类型不在当前结构中',
    'schema.field_path_exists': '字段路径有效',
    'schema.field_path_missing': '字段路径不在当前结构中',
    'schema.value_type_valid': '审核值类型符合字段定义',
    'schema.value_type_invalid': '审核值类型不符合字段定义',
    'schema.invalid_negative_value_type': '负例值类型不符合字段定义',
    'source.explicit_review': '存在明确人工审核动作',
    'source.review_fact_valid': '审核事实有效',
    'source.review_fact_invalid': '审核事实已禁用或失效',
    'evidence.visual_present': '存在字段级视觉证据',
    'evidence.visual_missing': '缺少字段级视觉证据',
    'evidence.readability': '字段视觉证据可读性',
    'evidence.image_resolution': '图片分辨率',
    'evidence.image_clarity': '图片清晰度',
    'evidence.page_quality_missing': '缺少图片质量数据',
    'vision.candidates_missing': '缺少视觉候选值',
    'vision.reviewed_value_consistency': '审核值与视觉候选的一致性',
    'ocr.not_available': '无可选 OCR 结果',
    'ocr.reviewed_value_consistency': '审核值与 OCR 候选的一致性',
    'business_rules.not_applicable': '没有适用的业务或格式校验',
    'conflict.input_scope_invalid': '冲突证据范围不一致',
    'conflict.same_evidence_open': '同一证据仍有未解决冲突',
    'conflict.open_review': '案例仍有未解决冲突',
    'conflict.none_open': '没有未解决冲突',
    'distribution.not_available': '暂无历史分布基线',
    'distribution.outlier_risk': '数值偏离历史分布',
    'distribution.within_observed_range': '数值处于历史观察范围',
    'text.prompt_injection_risk': '文本包含提示注入风险',
    'text.length_anomaly': '文本长度异常',
    'text.control_character_anomaly': '控制字符比例异常',
    'text.repetition_anomaly': '重复字符异常',
  })[signal.code] || localizedText(signal.message || signal.code)
}
function openAction(type, item = null, batch = false) { Object.assign(action, { open: true, type, item, batch }) }
async function decide(reason) {
  action.busy = true
  operationMessage.value = ''
  operationError.value = ''
  try {
    if (action.batch) {
      const result = await governanceApi.decideAdmissions(
        action.type,
        reason,
        selectedItems.value.map((item) => ({ admission_id: item.admission_id, expected_revision: item.revision })),
      )
      operationMessage.value = `${action.type === 'reassess' ? '批量重新评估已入队' : '批量操作完成'}：成功 ${result.succeeded_count} 条，失败 ${result.failed_count} 条`
      const failures = result.items.filter((item) => !item.succeeded)
      operationError.value = failures.map((item) => `${item.admission_id.slice(0, 8)}：${failureLabel(item)}`).join('；')
      if (failures.length) {
        notices.warning('批量治理部分完成', `成功 ${result.succeeded_count} 条，失败 ${result.failed_count} 条。失败项仍保持原状态。`)
      } else {
        notices.success('批量治理已完成', `已处理 ${result.succeeded_count} 条记忆准入记录。`, action.type === 'approve' ? {
          actionLabel: '查看案例库',
          onAction: () => emit('navigate', 'examples'),
        } : {})
      }
    } else {
      const result = await governanceApi.decideAdmission(action.item.admission_id, action.type, reason, action.item.revision)
      operationMessage.value = result.trace_id
        ? `${action.type === 'reassess' ? '重新评估已入队' : '治理决定已保存'}，技术关联标识：${result.trace_id}`
        : `${action.type === 'reassess' ? '重新评估已入队' : '治理决定已保存'}；该历史重放未采集技术关联标识`
      notices.success(`${actionLabel(action.type)}决定已保存`, '后端已记录治理事实，列表将读取最新状态。', action.type === 'approve' ? {
        actionLabel: '查看案例库',
        onAction: () => emit('navigate', 'examples'),
      } : {})
    }
    action.open = false
    await refresh(action.batch ? null : action.item.admission_id)
    if (pager.state.error || detailError.value) {
      notices.warning('操作已保存，但最新状态未读取', '请只刷新列表确认结果，不要重复提交治理决定。', {
        actionLabel: '重新读取',
        onAction: refresh,
      })
    }
    if (action.type === 'reassess') {
      notices.warning('等待 Worker 评估', 'pending 是临时状态；若质量检查仍失败，记录会再次被隔离。')
    }
  } catch (error) {
    action.open = false
    operationError.value = failureLabel(error)
    if (error instanceof ApiError && error.status === 409) {
      await refresh(action.batch ? null : action.item?.admission_id)
      if (pager.state.error || detailError.value) {
        notices.error('数据已变化，最新状态读取失败', '旧修订号已失效，请先刷新成功后再操作。', { duration: 0 })
      } else if (action.type === 'approve' && failedSignals.value.length) {
        operationError.value = `确定性质量检查未通过：${failedSignals.value.map(signalLabel).join('、')}`
        notices.warning('批准已被质量门禁阻止', '请核对当前证据与失败信号。')
      } else {
        notices.warning('数据已由其他治理人员更新', '已读取最新状态，请重新核对后提交。')
      }
    } else {
      await refresh(action.batch ? null : action.item?.admission_id)
      if (error instanceof ApiError && (error.status === 503 || error.status === 0)) {
        notices.warning('操作结果未确认', '服务端暂不可用，请核对最新状态，不要重复提交。', { duration: 0 })
      } else {
        notices.error('治理操作未完成', errorFeedback(error).message)
      }
    }
  } finally { action.busy = false }
}
onMounted(refresh)
</script>

<template>
  <div>
    <PageHeader eyebrow="记忆准入" title="记忆准入队列" description="客户审核是提交事实；只有通过独立准入的案例才可进入长期检索。">
      <button class="secondary-btn" @click="refresh()"><RefreshCw :size="16" />刷新</button>
    </PageHeader>
    <div v-if="operationMessage" class="alert neutral"><span>{{ operationMessage }}</span></div>
    <div v-if="operationError" class="alert error"><span>{{ operationError }}</span></div>
    <section class="toolbar"><label>准入状态<select v-model="status" @change="refresh()"><option value="all">全部状态</option><option v-for="item in ['pending','approved','quarantined','rejected','suspended','invalidated']" :key="item" :value="item">{{ displayLabel(item) }}</option></select></label><form class="admission-run-filter" @submit.prevent="applyRunFilter"><label>Run ID<input v-model="runIdDraft" maxlength="64" placeholder="完整 Run ID" /></label><label>字段路径<input v-model="fieldPathDraft" maxlength="512" placeholder="如 buyer_name" /></label><button class="secondary-btn" type="submit"><Search :size="15" />查询</button><button v-if="runIdFilter || fieldPathFilter" class="icon-btn" type="button" title="清除筛选" aria-label="清除筛选" @click="clearRunFilter"><X :size="17" /></button></form></section>
    <div v-if="runIdFilter || fieldPathFilter" class="alert neutral"><span>当前精确筛选：<code v-if="runIdFilter">{{ runIdFilter }}</code><code v-if="fieldPathFilter"> {{ fieldPathFilter }}</code><template v-if="runIdFilter">；合并案例显示首次来源 Run ID</template></span></div>
    <div class="governance-split">
      <section class="surface list-surface">
        <div class="batch-bar">
          <label class="selection-toggle"><input type="checkbox" :checked="allPageSelected" :disabled="!selectableItems.length" @change="togglePage($event.target.checked)"><span>本页全选</span></label>
          <span>已选 {{ selectedItems.length }} 条</span>
          <div>
            <button class="secondary-btn" :disabled="!actionAllowed('reassess')" @click="openAction('reassess', null, true)"><RotateCcw :size="15" />批量重新评估</button>
            <button class="secondary-btn" :disabled="!actionAllowed('quarantine')" @click="openAction('quarantine', null, true)"><ShieldAlert :size="15" />批量隔离</button>
            <button class="secondary-btn danger-text" :disabled="!actionAllowed('reject')" @click="openAction('reject', null, true)"><XCircle :size="15" />批量拒绝</button>
            <button class="primary-btn" :disabled="!actionAllowed('approve')" @click="openAction('approve', null, true)"><ShieldCheck :size="15" />批量批准</button>
          </div>
        </div>
        <ResourceState :loading="pager.state.loading" :error="listError" :empty="!pager.state.items.length" empty-text="当前筛选条件下没有准入记录" @retry="refresh()">
          <div class="record-list selectable">
            <div v-for="(item, index) in pager.state.items" :key="item.admission_id" class="record-row" :class="{ selected: selectedIds.has(item.admission_id) }">
              <label class="record-selector" @click.stop><input type="checkbox" :checked="selectedIds.has(item.admission_id)" :disabled="!['pending','quarantined'].includes(item.status)" @change="toggleSelection(item, $event.target.checked)"><span>{{ String(index + 1).padStart(2, '0') }}</span></label>
              <article @click="select(item)">
                <header><code>{{ item.field_path }}</code><StatusBadge :value="item.status" /></header>
                <p class="value-preview">解析值：{{ formatFieldValue(item.field_path, item.model_value) }}</p>
                <p>文档 {{ item.document_id.slice(0, 8) }} · Run {{ item.run_id.slice(0, 12) }} · 第 {{ item.evidence_reference?.page_number || '?' }} 页</p>
                <footer><span>{{ displayLabel(item.document_type, 'document_type') }} · {{ displayLabel(item.label_type, 'label_type') }}</span><span>修订号 {{ item.revision }} · {{ formatDate(item.updated_at) }}</span></footer>
              </article>
            </div>
          </div>
          <PaginationBar :count="pager.state.items.length" :can-previous="pager.state.history.length > 0" :can-next="Boolean(pager.state.nextCursor)" @previous="pager.previous" @next="pager.next" />
        </ResourceState>
      </section>
      <section class="surface detail-surface">
        <ResourceState :loading="detailLoading" :error="detailError" :empty="!detail" empty-text="选择一条记录查看质量信号、模型建议和最终决策" @retry="loadDetail()">
          <template v-if="detail">
            <header class="section-head"><div><span>准入详情</span><h2>{{ detail.field_path }}</h2></div><StatusBadge :value="detail.status" /></header>
            <div v-if="detail.status === 'rejected'" class="alert neutral"><span>该记录已由治理决定拒绝，当前没有重新评估或批准入口；此前的读取失败不会改变其状态。</span></div>
            <button class="secondary-btn" @click="runDialogId = runIdFilter || detail.run_id"><List :size="16" />查看本 Run 全部字段</button>
            <h3 class="subheading">解析与审核数据</h3>
            <div class="value-comparison"><div><span>模型解析值</span><strong>{{ formatFieldValue(detail.field_path, detail.model_value) }}</strong></div><div><span>人工审核值</span><strong>{{ formatFieldValue(detail.field_path, detail.reviewed_value) }}</strong></div></div>
            <div v-if="detail.evidence_reference?.candidate_values?.length" class="candidate-values"><span>图片证据候选</span><code v-for="value in detail.evidence_reference.candidate_values" :key="value">{{ fullText(value) }}</code></div>
            <details class="technical-details">
              <summary>查看证据、版本与治理元数据</summary>
              <dl class="meta-list columns"><div><dt>文档编号</dt><dd>{{ detail.document_id }}</dd></div><div><dt>运行编号</dt><dd>{{ detail.run_id }}</dd></div><div><dt>页码</dt><dd>{{ detail.evidence_reference?.page_number || '未记录' }}</dd></div><div><dt>证据来源</dt><dd>{{ displayLabel(detail.evidence_reference?.evidence_source, 'evidence_source') }}</dd></div><div><dt>模型版本</dt><dd>{{ detail.model_version || '未记录' }}</dd></div><div><dt>提示词版本</dt><dd>{{ detail.prompt_version || '未记录' }}</dd></div><div><dt>文档引用</dt><dd>{{ detail.evidence_reference?.document_reference || '未记录' }}</dd></div><div><dt>图片引用</dt><dd>{{ detail.evidence_reference?.image_reference || '未记录' }}</dd></div><div><dt>可读性</dt><dd>{{ displayLabel(detail.evidence_reference?.readability, 'readability') }}</dd></div><div><dt>是否歧义</dt><dd>{{ detail.evidence_reference?.ambiguous ? '是' : '否' }}</dd></div><div><dt>修正原因</dt><dd>{{ fullText(detail.correction_reason) }}</dd></div><div><dt>验证信号</dt><dd>{{ localizedText(detail.evidence_reference?.validation_signals?.join(' · ')) }}</dd></div></dl>
              <h3 class="subheading">治理元数据</h3>
              <dl class="meta-list columns"><div><dt>准入记录</dt><dd>{{ detail.admission_id }}</dd></div><div><dt>原审核人</dt><dd>{{ detail.source_reviewer_id }}</dd></div><div><dt>策略版本</dt><dd>{{ detail.policy_version }}</dd></div><div><dt>数据结构版本</dt><dd>{{ detail.schema_version }}</dd></div><div><dt>修订号</dt><dd>{{ detail.revision }}</dd></div><div><dt>开放冲突</dt><dd>{{ detail.open_conflicts?.length || 0 }}</dd></div></dl>
            </details>
            <h3 class="subheading">质量评估（分数不是概率）</h3>
            <div v-if="failedSignals.length" class="alert error"><span>当前确定性质量检查未通过：{{ failedSignals.map(signalLabel).join('、') }}。不能批准此案例。</span></div>
            <article v-for="assessment in detail.assessments" :key="assessment.assessment_id" class="assessment"><header><strong>{{ displayLabel(assessment.source, 'assessment_source') }}</strong><StatusBadge :value="assessment.recommendation" /></header><div class="score-line">质量分数 {{ formatScore(assessment.quality_score) }} · {{ assessment.advisory_only ? '仅模型建议' : '确定性评估' }}</div><div class="signal-list"><div v-for="signal in assessment.signals" :key="`${assessment.assessment_id}-${signal.code}`"><StatusBadge :value="signal.verdict" /><span>{{ signalLabel(signal) }}</span></div></div><footer>风险依据 {{ assessment.reason_codes.length }} 项 · {{ assessment.model_version || '无模型' }} · {{ assessment.prompt_version || '无提示词版本' }} · {{ formatDate(assessment.assessed_at) }}</footer></article>
            <h3 v-if="detail.open_conflicts?.length" class="subheading">开放冲突</h3>
            <div v-if="detail.open_conflicts?.length" class="record-list"><article v-for="conflict in detail.open_conflicts" :key="conflict.conflict_id"><header><strong>{{ displayLabel(conflict.conflict_type, 'conflict_type') }}</strong><StatusBadge :value="conflict.status" /></header><p>风险依据 {{ conflict.reason_codes.length }} 项；候选字段 {{ conflict.candidate_field_paths.join('、') || '未记录' }}</p><footer>证据引用 {{ conflict.evidence_references.length }} 条（敏感原值隐藏）</footer></article></div>
            <h3 class="subheading">最终决策历史</h3>
            <div class="timeline"><div v-for="decision in detail.decisions" :key="decision.decision_id"><StatusBadge :value="decision.status" /><div><strong>{{ decision.decided_by }}</strong><p>{{ decision.reason }}</p><small>修订号 {{ decision.revision }} · {{ formatDate(decision.decided_at) }}</small></div></div></div>
            <div v-if="['pending','quarantined'].includes(detail.status)" class="button-row"><button v-if="detail.status === 'quarantined'" class="secondary-btn" @click="openAction('reassess', detail)"><RotateCcw :size="15" />重新评估</button><button v-if="detail.status === 'pending'" class="secondary-btn" @click="openAction('quarantine', detail)"><ShieldAlert :size="15" />隔离</button><button class="secondary-btn danger-text" @click="openAction('reject', detail)"><XCircle :size="15" />拒绝</button><button class="primary-btn" :disabled="failedSignals.length > 0" :title="failedSignals.length ? '确定性质量检查未通过' : undefined" @click="openAction('approve', detail)"><ShieldCheck :size="15" />批准准入</button></div>
          </template>
        </ResourceState>
      </section>
    </div>
    <ActionDialog :open="action.open" :busy="action.busy" :danger="action.type !== 'approve'" :revision="action.batch ? null : action.item?.revision ?? null" :item-count="action.batch ? selectedItems.length : 1" :title="`${action.batch ? '批量' : ''}${actionLabel(action.type)}记忆案例`" :confirm-label="action.type === 'reject' ? '确认拒绝（当前不可恢复）' : '提交治理决定'" @close="action.open = false" @confirm="decide" />
    <RunFieldsDialog :open="Boolean(runDialogId)" :run-id="runDialogId" @close="runDialogId = ''" />
  </div>
</template>
