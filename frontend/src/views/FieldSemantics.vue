<script setup>
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { RefreshCw, Search, X } from 'lucide-vue-next'
import ActionDialog from '../components/ActionDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import PaginationBar from '../components/PaginationBar.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { displayLabel, formatDate } from '../api/client'
import { governanceApi } from '../api/governance'
import { useNotifications } from '../composables/useNotifications'

const notices = useNotifications()

const filter = ref('pending')
const pathDraft = ref('')
const pathFilter = ref('')
const operationMessage = ref('')
const state = reactive({ payload: null, loading: false, error: '', cursor: null, history: [] })
const action = reactive({ open: false, type: '', item: null, busy: false })
let loadController = null
async function load(reset = false) {
  loadController?.abort()
  if (reset) { state.cursor = null; state.history = [] }
  const controller = new AbortController()
  loadController = controller
  state.loading = true
  state.error = ''
  try {
    state.payload = await governanceApi.fieldSemantics(
      { alias_status: filter.value === 'all' ? undefined : filter.value, canonical_field_path: pathFilter.value, limit: 50, cursor: state.cursor },
      { signal: controller.signal },
    )
  } catch (error) {
    if (!controller.signal.aborted) state.error = error.message
  } finally {
    if (loadController === controller) {
      loadController = null
      state.loading = false
    }
  }
}
function applySearch() { pathFilter.value = pathDraft.value.trim(); load(true) }
function clearSearch() { pathDraft.value = ''; applySearch() }
async function next() { if (!state.payload?.next_cursor) return; state.history.push(state.cursor); state.cursor = state.payload.next_cursor; await load() }
async function previous() { if (!state.history.length) return; state.cursor = state.history.pop() ?? null; await load() }
function openAction(type, item) { Object.assign(action, { open: true, type, item }) }
async function decide(reason) {
  action.busy = true
  try {
    const result = await governanceApi.decideAlias(
      action.item.alias_id,
      action.type,
      reason,
      action.item.revision,
    )
    operationMessage.value = result.trace_id
      ? `别名治理决定已保存，技术关联标识：${result.trace_id}`
      : '别名治理决定已保存；该历史重放未采集技术关联标识'
    notices.success(action.type === 'approve' ? '字段别名已批准' : '字段别名已禁用', action.type === 'approve'
      ? '后端已登记新目录版本；只有有效版本会进入字段语义索引。'
      : '后端已记录禁用决定，派生索引将按治理流程失效。')
    action.open = false
    await load()
    if (state.error) {
      notices.warning('操作已保存，但最新状态未读取', '请只重新读取别名列表，不要重复提交治理决定。', {
        actionLabel: '重新读取',
        onAction: () => load(true),
      })
    }
  } catch (error) {
    action.open = false
    if (error.status === 409) {
      action.item = null
      await load(true)
      if (state.error) {
        notices.error('数据已变化，最新状态读取失败', '旧修订号已失效，请先刷新成功后再操作。', { duration: 0 })
      } else {
        state.error = '数据已由其他治理人员更新，请核对最新修订号后重试'
        notices.warning('别名记录已更新', '已读取最新修订号，请重新核对后提交。')
      }
    } else {
      state.error = error.message
      notices.error('别名治理未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
    }
  } finally {
    action.busy = false
  }
}
onMounted(() => load(true))
onBeforeUnmount(() => loadController?.abort())
</script>

<template>
  <div>
    <PageHeader eyebrow="字段语义" title="字段语义治理" description="基础定义来自只读发票数据结构；租户别名版本化并经审批。"><button class="secondary-btn" @click="load(true)"><RefreshCw :size="16" />刷新</button></PageHeader>
    <div v-if="operationMessage" class="alert neutral"><span>{{ operationMessage }}</span></div>
    <section class="toolbar"><label>候选状态<select v-model="filter" @change="load(true)"><option value="all">全部状态</option><option v-for="item in ['pending','approved','rejected','suspended','invalidated']" :key="item" :value="item">{{ displayLabel(item, 'alias_status') }}</option></select></label><form class="admission-run-filter" @submit.prevent="applySearch"><label>标准字段路径<input v-model="pathDraft" maxlength="512" placeholder="如 buyer_name" /></label><button class="secondary-btn" type="submit"><Search :size="15" />查询</button><button v-if="pathFilter" class="icon-btn" type="button" title="清除筛选" @click="clearSearch"><X :size="17" /></button></form></section>
    <ResourceState :loading="state.loading" :error="state.error" :empty="!state.payload" @retry="load">
      <div v-if="state.payload" class="semantics-grid">
        <section class="surface"><header class="section-head"><div><span>标准字段定义</span><h2>当前字段目录</h2></div><b>{{ state.payload.definitions.length }}</b></header><div class="definition-list"><article v-for="item in state.payload.definitions" :key="`${item.catalog_version}-${item.canonical_field_path}`"><header><code>{{ item.canonical_field_path }}</code><StatusBadge :value="item.is_valid ? 'completed' : 'invalidated'" /></header><h3>{{ item.display_name }}</h3><p>{{ item.description }}</p><div class="tag-row"><span>类型 {{ displayLabel(item.value_type, 'value_type') }}</span><span>目录版本 {{ item.catalog_version }}</span><span>数据结构版本 {{ item.schema_version }}</span></div><dl><div><dt>已审批别名</dt><dd>{{ item.aliases.map((alias) => alias.alias_text).join('、') || '无' }}</dd></div><div><dt>负向别名</dt><dd>{{ item.negative_aliases.map((alias) => alias.alias_text).join('、') || '无' }}</dd></div><div><dt>上下文锚点</dt><dd>{{ item.context_anchors.map((anchor) => anchor.text).join('、') || '无' }}</dd></div></dl></article></div></section>
        <section class="surface"><header class="section-head"><div><span>租户别名候选</span><h2>待治理别名</h2></div><b>{{ state.payload.alias_candidates.length }}</b></header><div class="record-list"><article v-for="item in state.payload.alias_candidates" :key="item.alias_id"><header><strong>{{ item.alias_text }}</strong><StatusBadge :value="item.status" /></header><p>映射到标准字段：<code>{{ item.canonical_field_path }}</code></p><div class="support-grid"><span>不同文档 <b>{{ item.support.distinct_documents }}</b></span><span>不同模板 <b>{{ item.support.distinct_templates }}</b></span><span>不同审核人 <b>{{ item.support.distinct_reviewers }}</b></span><span>支持来源 <b>{{ item.support.source_count }}</b></span></div><footer><span>策略版本 {{ item.policy_version }} · 修订号 {{ item.revision }} · {{ formatDate(item.support.window_started_at) }} 至 {{ formatDate(item.support.window_ended_at) }}</span><span>{{ formatDate(item.updated_at) }}</span></footer><div v-if="['pending','approved'].includes(item.status)" class="button-row"><button v-if="item.status === 'approved'" class="secondary-btn danger-text" @click="openAction('disable', item)">禁用</button><button v-if="item.status === 'pending'" class="primary-btn" :disabled="item.canonical_collision" @click="openAction('approve', item)">审批别名</button></div></article><p v-if="!state.payload.alias_candidates.length" class="empty-row">当前无候选</p></div></section>
      </div>
      <PaginationBar v-if="state.payload" :count="state.payload.alias_candidates.length" :can-previous="state.history.length > 0" :can-next="Boolean(state.payload.next_cursor)" @previous="previous" @next="next" />
    </ResourceState>
    <ActionDialog :open="action.open" :busy="action.busy" :danger="action.type === 'disable'" :revision="action.item?.revision ?? null" :title="`${action.type === 'approve' ? '审批' : '禁用'}租户别名`" confirm-label="提交决定" @close="action.open = false" @confirm="decide" />
  </div>
</template>
