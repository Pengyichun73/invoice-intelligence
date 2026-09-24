<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { RefreshCw, RotateCcw, ShieldAlert, ShieldCheck, XCircle } from 'lucide-vue-next'
import ActionDialog from '../components/ActionDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import PaginationBar from '../components/PaginationBar.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { ApiError, displayLabel, formatDate, formatScore, fullText, localizedText } from '../api/client'
import { governanceApi } from '../api/governance'
import { usePagedResource } from '../composables/usePagedResource'
import { useNotifications } from '../composables/useNotifications'

const emit = defineEmits(['navigate'])
const notices = useNotifications()

const status = ref('pending')
const detail = ref(null)
const detailLoading = ref(false)
const operationMessage = ref('')
const operationError = ref('')
const selectedIds = ref(new Set())
const action = reactive({ open: false, type: '', item: null, batch: false, busy: false })
const pager = usePagedResource((cursor) => governanceApi.admissions({ status: status.value, limit: 20, cursor }))
const selectedItems = computed(() => pager.state.items.filter((item) => selectedIds.value.has(item.admission_id)))
const selectableItems = computed(() => pager.state.items.filter((item) => ['pending', 'quarantined'].includes(item.status)))
const allPageSelected = computed(() => selectableItems.value.length > 0 && selectableItems.value.every((item) => selectedIds.value.has(item.admission_id)))

async function refresh() { detail.value = null; selectedIds.value = new Set(); await pager.load({ reset: true }).catch(() => {}) }
async function select(item) {
  detailLoading.value = true
  try { detail.value = await governanceApi.admission(item.admission_id) }
  catch (error) { pager.state.error = error.message }
  finally { detailLoading.value = false }
}
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
  const allowed = {
    approve: ['pending', 'quarantined'],
    reassess: ['quarantined'],
    quarantine: ['pending'],
    reject: ['pending', 'quarantined'],
  }
  return selectedItems.value.length > 0 && selectedItems.value.every((item) => allowed[type].includes(item.status))
}
function actionLabel(type) { return ({ approve: '批准', reassess: '重新评估', quarantine: '隔离', reject: '拒绝' })[type] || type }
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
      operationMessage.value = `批量操作完成：成功 ${result.succeeded_count} 条，失败 ${result.failed_count} 条`
      const failures = result.items.filter((item) => !item.succeeded)
      operationError.value = failures.map((item) => `${item.admission_id.slice(0, 8)}：未处理，请刷新后核对状态`).join('；')
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
        ? `治理决定已保存，技术关联标识：${result.trace_id}`
        : '治理决定已保存；该历史重放未采集技术关联标识'
      notices.success(`${actionLabel(action.type)}决定已保存`, '后端已记录治理事实，列表将读取最新状态。', action.type === 'approve' ? {
        actionLabel: '查看案例库',
        onAction: () => emit('navigate', 'examples'),
      } : {})
    }
    action.open = false
    await refresh()
    if (pager.state.error) {
      notices.warning('操作已保存，但最新状态未读取', '请只刷新列表确认结果，不要重复提交治理决定。', {
        actionLabel: '重新读取',
        onAction: refresh,
      })
    }
  } catch (error) {
    action.open = false
    pager.state.error = error.message
    if (error instanceof ApiError && error.status === 409) {
      detail.value = null
      selectedIds.value = new Set()
      await refresh()
      if (pager.state.error) {
        notices.error('数据已变化，最新状态读取失败', '旧修订号已失效，请先刷新成功后再操作。', { duration: 0 })
      } else {
        notices.warning('数据已由其他治理人员更新', '已读取最新状态，请重新核对后提交。')
      }
    } else {
      notices.error('治理操作未完成', error instanceof ApiError ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
    }
  } finally { action.busy = false }
}
onMounted(refresh)
</script>

<template>
  <div>
    <PageHeader eyebrow="记忆准入" title="记忆准入队列" description="客户审核是提交事实；只有通过独立准入的案例才可进入长期检索。">
      <button class="secondary-btn" @click="refresh"><RefreshCw :size="16" />刷新</button>
    </PageHeader>
    <div v-if="operationMessage" class="alert neutral"><span>{{ operationMessage }}</span></div>
    <div v-if="operationError" class="alert error"><span>{{ operationError }}</span></div>
    <section class="toolbar"><label>准入状态<select v-model="status" @change="refresh"><option v-for="item in ['pending','approved','quarantined','rejected','suspended','invalidated']" :key="item" :value="item">{{ displayLabel(item) }}</option></select></label><span class="context-note"><ShieldCheck :size="15" />审批人由后端可信上下文确定</span></section>
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
        <ResourceState :loading="pager.state.loading" :error="pager.state.error" :empty="!pager.state.items.length" empty-text="当前筛选条件下没有准入记录" @retry="refresh">
          <div class="record-list selectable">
            <div v-for="(item, index) in pager.state.items" :key="item.admission_id" class="record-row" :class="{ selected: selectedIds.has(item.admission_id) }">
              <label class="record-selector" @click.stop><input type="checkbox" :checked="selectedIds.has(item.admission_id)" :disabled="!['pending','quarantined'].includes(item.status)" @change="toggleSelection(item, $event.target.checked)"><span>{{ String(index + 1).padStart(2, '0') }}</span></label>
              <article @click="select(item)">
                <header><code>{{ item.field_path }}</code><StatusBadge :value="item.status" /></header>
                <p class="value-preview">解析值：{{ fullText(item.model_value) }}</p>
                <p>文档 {{ item.document_id.slice(0, 8) }} · Run {{ item.run_id.slice(0, 12) }} · 第 {{ item.evidence_reference?.page_number || '?' }} 页</p>
                <footer><span>{{ displayLabel(item.document_type, 'document_type') }} · {{ displayLabel(item.label_type, 'label_type') }}</span><span>修订号 {{ item.revision }} · {{ formatDate(item.updated_at) }}</span></footer>
              </article>
            </div>
          </div>
          <PaginationBar :count="pager.state.items.length" :can-previous="pager.state.history.length > 0" :can-next="Boolean(pager.state.nextCursor)" @previous="pager.previous" @next="pager.next" />
        </ResourceState>
      </section>
      <section class="surface detail-surface">
        <ResourceState :loading="detailLoading" :empty="!detail" empty-text="选择一条记录查看质量信号、模型建议和最终决策">
          <template v-if="detail">
            <header class="section-head"><div><span>准入详情</span><h2>{{ detail.field_path }}</h2></div><StatusBadge :value="detail.status" /></header>
            <h3 class="subheading">解析与审核数据</h3>
            <div class="value-comparison"><div><span>模型解析值</span><strong>{{ fullText(detail.model_value) }}</strong></div><div><span>人工审核值</span><strong>{{ fullText(detail.reviewed_value) }}</strong></div></div>
            <div v-if="detail.evidence_reference?.candidate_values?.length" class="candidate-values"><span>图片证据候选</span><code v-for="value in detail.evidence_reference.candidate_values" :key="value">{{ fullText(value) }}</code></div>
            <details class="technical-details">
              <summary>查看证据、版本与治理元数据</summary>
              <dl class="meta-list columns"><div><dt>文档编号</dt><dd>{{ detail.document_id }}</dd></div><div><dt>运行编号</dt><dd>{{ detail.run_id }}</dd></div><div><dt>页码</dt><dd>{{ detail.evidence_reference?.page_number || '未记录' }}</dd></div><div><dt>证据来源</dt><dd>{{ displayLabel(detail.evidence_reference?.evidence_source, 'evidence_source') }}</dd></div><div><dt>模型版本</dt><dd>{{ detail.model_version || '未记录' }}</dd></div><div><dt>提示词版本</dt><dd>{{ detail.prompt_version || '未记录' }}</dd></div><div><dt>文档引用</dt><dd>{{ detail.evidence_reference?.document_reference || '未记录' }}</dd></div><div><dt>图片引用</dt><dd>{{ detail.evidence_reference?.image_reference || '未记录' }}</dd></div><div><dt>可读性</dt><dd>{{ detail.evidence_reference?.readability || '未记录' }}</dd></div><div><dt>是否歧义</dt><dd>{{ detail.evidence_reference?.ambiguous ? '是' : '否' }}</dd></div><div><dt>修正原因</dt><dd>{{ localizedText(detail.correction_reason) }}</dd></div><div><dt>验证信号</dt><dd>{{ localizedText(detail.evidence_reference?.validation_signals?.join(' · ')) }}</dd></div></dl>
              <h3 class="subheading">治理元数据</h3>
              <dl class="meta-list columns"><div><dt>准入记录</dt><dd>{{ detail.admission_id }}</dd></div><div><dt>原审核人</dt><dd>{{ detail.source_reviewer_id }}</dd></div><div><dt>策略版本</dt><dd>{{ detail.policy_version }}</dd></div><div><dt>数据结构版本</dt><dd>{{ detail.schema_version }}</dd></div><div><dt>修订号</dt><dd>{{ detail.revision }}</dd></div><div><dt>开放冲突</dt><dd>{{ detail.open_conflicts?.length || 0 }}</dd></div></dl>
            </details>
            <h3 class="subheading">质量评估（分数不是概率）</h3>
            <article v-for="assessment in detail.assessments" :key="assessment.assessment_id" class="assessment"><header><strong>{{ displayLabel(assessment.source, 'assessment_source') }}</strong><StatusBadge :value="assessment.recommendation" /></header><div class="score-line">质量分数 {{ formatScore(assessment.quality_score) }} · {{ assessment.advisory_only ? '仅模型建议' : '确定性评估' }}</div><div class="signal-list"><div v-for="signal in assessment.signals" :key="`${assessment.assessment_id}-${signal.code}`"><StatusBadge :value="signal.verdict" /><span>{{ localizedText(signal.message) }}</span></div></div><footer>风险依据 {{ assessment.reason_codes.length }} 项 · {{ assessment.model_version || '无模型' }} · {{ assessment.prompt_version || '无提示词版本' }} · {{ formatDate(assessment.assessed_at) }}</footer></article>
            <h3 v-if="detail.open_conflicts?.length" class="subheading">开放冲突</h3>
            <div v-if="detail.open_conflicts?.length" class="record-list"><article v-for="conflict in detail.open_conflicts" :key="conflict.conflict_id"><header><strong>{{ displayLabel(conflict.conflict_type, 'conflict_type') }}</strong><StatusBadge :value="conflict.status" /></header><p>风险依据 {{ conflict.reason_codes.length }} 项；候选字段 {{ conflict.candidate_field_paths.join('、') || '未记录' }}</p><footer>证据引用 {{ conflict.evidence_references.length }} 条（敏感原值隐藏）</footer></article></div>
            <h3 class="subheading">最终决策历史</h3>
            <div class="timeline"><div v-for="decision in detail.decisions" :key="decision.decision_id"><StatusBadge :value="decision.status" /><div><strong>{{ decision.decided_by }}</strong><p>{{ decision.reason }}</p><small>修订号 {{ decision.revision }} · {{ formatDate(decision.decided_at) }}</small></div></div></div>
            <div v-if="['pending','quarantined'].includes(detail.status)" class="button-row"><button v-if="detail.status === 'quarantined'" class="secondary-btn" @click="openAction('reassess', detail)"><RotateCcw :size="15" />重新评估</button><button v-if="detail.status === 'pending'" class="secondary-btn" @click="openAction('quarantine', detail)"><ShieldAlert :size="15" />隔离</button><button class="secondary-btn danger-text" @click="openAction('reject', detail)"><XCircle :size="15" />拒绝</button><button class="primary-btn" @click="openAction('approve', detail)"><ShieldCheck :size="15" />批准准入</button></div>
          </template>
        </ResourceState>
      </section>
    </div>
    <ActionDialog :open="action.open" :busy="action.busy" :danger="action.type !== 'approve'" :revision="action.batch ? null : action.item?.revision ?? null" :item-count="action.batch ? selectedItems.length : 1" :title="`${action.batch ? '批量' : ''}${actionLabel(action.type)}记忆案例`" confirm-label="提交治理决定" @close="action.open = false" @confirm="decide" />
  </div>
</template>
