<script setup>
import { onMounted, reactive, ref } from 'vue'
import { RefreshCw } from 'lucide-vue-next'
import ActionDialog from '../components/ActionDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import PaginationBar from '../components/PaginationBar.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { displayLabel, formatDate, fullText, safeText } from '../api/client'
import { governanceApi } from '../api/governance'
import { usePagedResource } from '../composables/usePagedResource'
import { useNotifications } from '../composables/useNotifications'

const notices = useNotifications()

const labelType = ref('')
const validFilter = ref('true')
const detail = ref(null)
const selectedExampleId = ref('')
const detailLoading = ref(false)
const projections = reactive({ payload: null, loading: false, error: '', cursor: null, history: [] })
const action = reactive({ open: false, item: null, busy: false })
const pager = usePagedResource((cursor) => governanceApi.examples({ label_type: labelType.value, is_valid: validFilter.value === '' ? null : validFilter.value, limit: 20, cursor }))
async function refresh() {
  detail.value = null
  selectedExampleId.value = ''
  Object.assign(projections, { payload: null, loading: false, error: '', cursor: null, history: [] })
  await pager.load({ reset: true }).catch(() => {})
}
async function loadProjections(reset = false) {
  if (!selectedExampleId.value) return
  if (reset) {
    projections.cursor = null
    projections.history = []
  }
  projections.loading = true
  projections.error = ''
  try {
    projections.payload = await governanceApi.exampleProjections(
      selectedExampleId.value,
      { limit: 10, cursor: projections.cursor },
    )
  } catch (error) {
    projections.error = error.message
  } finally {
    projections.loading = false
  }
}
async function nextProjectionPage() {
  if (!projections.payload?.next_cursor) return
  projections.history.push(projections.cursor)
  projections.cursor = projections.payload.next_cursor
  await loadProjections()
}
async function previousProjectionPage() {
  if (!projections.history.length) return
  projections.cursor = projections.history.pop() ?? null
  await loadProjections()
}
async function select(item) {
  detailLoading.value = true
  selectedExampleId.value = item.example_id
  projections.payload = null
  projections.error = ''
  try {
    detail.value = await governanceApi.example(item.example_id)
    await loadProjections(true)
  } catch (error) { pager.state.error = error.message }
  finally { detailLoading.value = false }
}
async function disable(reason) {
  action.busy = true
  try {
    await governanceApi.disableExample(action.item.example_id, reason)
    action.open = false
    notices.success('案例已禁用', '权威案例状态已更新，派生索引将按治理流程清理。')
    await refresh()
    if (pager.state.error) {
      notices.warning('案例已禁用，但最新列表未读取', '请只刷新案例列表确认结果，不要重复提交禁用操作。', {
        actionLabel: '重新读取',
        onAction: refresh,
      })
    }
  }
  catch (error) {
    pager.state.error = error.message
    action.open = false
    notices.error('案例禁用未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  }
  finally { action.busy = false }
}
onMounted(refresh)
</script>

<template>
  <div>
    <PageHeader eyebrow="案例治理" title="案例库" description="正例、纠错例与确认错误案例严格分区展示。"><button class="secondary-btn" @click="refresh"><RefreshCw :size="16" />刷新</button></PageHeader>
    <section class="toolbar"><label>案例类型<select v-model="labelType" @change="refresh"><option value="">全部类型</option><option value="confirmed_correct">确认正确</option><option value="corrected">人工纠正</option><option value="confirmed_incorrect">确认错误（负例）</option></select></label><label>有效状态<select v-model="validFilter" @change="refresh"><option value="true">有效</option><option value="false">已禁用/失效</option><option value="">全部</option></select></label><span class="context-note">负例不会进入正确案例区域</span></section>
    <div class="governance-split">
      <section class="surface list-surface">
        <ResourceState :loading="pager.state.loading" :error="pager.state.error" :empty="!pager.state.items.length" @retry="refresh">
          <div class="example-groups">
            <section><h3>正确与纠错案例</h3><div class="record-list selectable"><article v-for="item in pager.state.items.filter((entry) => entry.label_type !== 'confirmed_incorrect')" :key="item.example_id" @click="select(item)"><header><code>{{ item.field_path }}</code><StatusBadge :value="item.label_type" /></header><p>{{ displayLabel(item.document_type, 'document_type') }} · 已记录审核来源</p><footer><span>出现 {{ item.occurrence_count }} 次</span><span>{{ formatDate(item.last_seen_at) }}</span></footer></article><p v-if="!pager.state.items.some((entry) => entry.label_type !== 'confirmed_incorrect')" class="empty-row">本页无正例或纠错例</p></div></section>
            <section class="negative-zone"><h3>确认错误案例（仅负例）</h3><div class="record-list selectable"><article v-for="item in pager.state.items.filter((entry) => entry.label_type === 'confirmed_incorrect')" :key="item.example_id" @click="select(item)"><header><code>{{ item.field_path }}</code><StatusBadge value="confirmed_incorrect" /></header><p>错误原因：{{ fullText(item.correction_reason) }}</p><footer>{{ formatDate(item.last_seen_at) }}</footer></article><p v-if="!pager.state.items.some((entry) => entry.label_type === 'confirmed_incorrect')" class="empty-row">本页无负例</p></div></section>
          </div>
          <PaginationBar :count="pager.state.items.length" :can-previous="pager.state.history.length > 0" :can-next="Boolean(pager.state.nextCursor)" @previous="pager.previous" @next="pager.next" />
        </ResourceState>
      </section>
      <section class="surface detail-surface">
        <ResourceState :loading="detailLoading" :empty="!detail" empty-text="选择案例查看详情">
          <template v-if="detail">
            <header class="section-head"><div><span>案例详情</span><h2>{{ detail.field_path }}</h2></div><StatusBadge :value="detail.label_type" /></header>
            <dl class="meta-list">
              <div><dt>记忆准入</dt><dd><StatusBadge :value="projections.payload?.admission_status || '未加载'" /></dd></div>
              <div><dt>长期检索资格</dt><dd>{{ projections.payload?.eligible_for_long_term_retrieval ? '可参与' : '不可参与' }}</dd></div>
              <div><dt>文档编号</dt><dd>{{ detail.document_id }}</dd></div>
              <div><dt>运行编号</dt><dd>{{ detail.run_id }}</dd></div>
              <div><dt>来源事件</dt><dd>{{ detail.source_event_id || '未记录' }}</dd></div>
              <div><dt>审核人</dt><dd>{{ detail.reviewer_id }}</dd></div>
              <div><dt>数据结构 / 目录版本</dt><dd>{{ detail.schema_version }} / {{ detail.catalog_version }}</dd></div>
              <div><dt>模型 / 提示词版本</dt><dd>{{ detail.model_version }} / {{ detail.prompt_version }}</dd></div>
              <div><dt>证据来源</dt><dd>第 {{ detail.evidence_reference?.page_number || '-' }} 页 · {{ displayLabel(detail.evidence_reference?.evidence_source, 'evidence_source') }}</dd></div>
            </dl>
            <h3 class="subheading">完整案例数据</h3>
            <div class="value-comparison"><div><span>模型解析值</span><strong>{{ fullText(detail.model_value) }}</strong></div><div><span>人工审核值</span><strong>{{ fullText(detail.reviewed_value) }}</strong></div></div>
            <div v-if="detail.correction_reason" class="full-reason"><strong>审核原因</strong><p>{{ fullText(detail.correction_reason) }}</p></div>
            <section class="projection-section">
              <div class="section-head"><div><span>索引投影状态</span><h3>案例索引投影</h3></div><button class="secondary-btn" :disabled="projections.loading" @click="loadProjections(true)"><RefreshCw :size="15" />刷新</button></div>
              <p class="context-note">状态来自 PostgreSQL；“已登记索引”表示记录过一次成功投影，不等于实时 Milvus 健康检查。</p>
              <ResourceState :loading="projections.loading" :error="projections.error" :empty="!projections.payload?.items?.length" empty-text="该案例没有投影记录" @retry="loadProjections">
                <div v-if="projections.payload" class="record-list">
                  <article v-for="item in projections.payload.items" :key="item.index_version">
                    <header><code>{{ item.index_version }}</code><StatusBadge :value="item.projection_status" /></header>
                    <p>尝试 {{ item.attempt_count }} 次 · 校验摘要 {{ safeText(item.projection_checksum, 20) }}</p>
                    <p v-if="item.last_error_code">安全错误码：{{ item.last_error_code }}</p>
                    <footer><span>更新 {{ formatDate(item.updated_at) }}</span><span>完成 {{ formatDate(item.indexed_at) }}</span></footer>
                  </article>
                </div>
                <PaginationBar v-if="projections.payload" :count="projections.payload.items.length" :can-previous="projections.history.length > 0" :can-next="Boolean(projections.payload.next_cursor)" @previous="previousProjectionPage" @next="nextProjectionPage" />
              </ResourceState>
            </section>
            <button v-if="detail.is_valid" class="secondary-btn danger-text" @click="Object.assign(action, { open: true, item: detail })">禁用错误案例</button>
          </template>
        </ResourceState>
      </section>
    </div>
    <ActionDialog :open="action.open" :busy="action.busy" danger title="禁用案例" confirm-label="确认禁用" @close="action.open = false" @confirm="disable" />
  </div>
</template>
