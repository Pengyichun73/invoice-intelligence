<script setup>
import { onMounted, ref } from 'vue'
import { Check, Copy, RefreshCw } from 'lucide-vue-next'
import PageHeader from '../components/PageHeader.vue'
import PaginationBar from '../components/PaginationBar.vue'
import ResourceState from '../components/ResourceState.vue'
import { displayLabel, formatDate } from '../api/client'
import { governanceApi } from '../api/governance'
import { usePagedResource } from '../composables/usePagedResource'

const operation = ref('')
const resourceType = ref('')
const resourceId = ref('')
const traceId = ref('')
const startedAt = ref('')
const endedAt = ref('')
const copiedTrace = ref('')
const operations = [
  'approve_admission', 'reject_admission', 'quarantine_admission',
  'requeue_admission',
  'approve_field_alias', 'disable_field_alias', 'resolve_conflict',
  'dismiss_conflict', 'disable_example', 'invalidate_schema',
  'rebuild_index', 'submit_feedback',
]
const resourceTypes = ['memory_admission', 'field_alias', 'memory_conflict', 'reviewed_example', 'index_version', 'schema']

function toIso(value) {
  if (!value) return null
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString()
}

const pager = usePagedResource((cursor) => governanceApi.audits({
  operation: operation.value,
  resource_type: resourceType.value.trim(),
  resource_id: resourceId.value.trim(),
  trace_id: traceId.value.trim(),
  started_at: toIso(startedAt.value),
  ended_at: toIso(endedAt.value),
  limit: 25,
  cursor,
}))

async function refresh() {
  copiedTrace.value = ''
  await pager.load({ reset: true }).catch(() => {})
}

async function copyTrace(value) {
  if (!value) return
  await navigator.clipboard.writeText(value)
  copiedTrace.value = value
}

onMounted(refresh)
</script>

<template>
  <div>
    <PageHeader eyebrow="治理审计" title="审计记录" description="PostgreSQL 中不可变、租户隔离且可归因的治理操作；不包含敏感内容。">
      <button class="secondary-btn" :disabled="pager.state.loading" @click="refresh"><RefreshCw :size="16" />刷新</button>
    </PageHeader>
    <div class="alert neutral"><span>技术关联标识仅用于请求与日志关联，不是业务编号。历史记录未采集的版本和关联标识保持为空。</span></div>
    <details class="technical-details audit-filter-details">
      <summary>高级筛选</summary>
      <section class="toolbar audit-filters">
      <label>操作<select v-model="operation" @change="refresh"><option value="">全部操作</option><option v-for="item in operations" :key="item" :value="item">{{ displayLabel(item, 'operation') }}</option></select></label>
      <label>资源类型<select v-model="resourceType" @change="refresh"><option value="">全部资源</option><option v-for="item in resourceTypes" :key="item" :value="item">{{ displayLabel(item, 'resource_type') }}</option></select></label>
      <label>资源 ID<input v-model="resourceId" maxlength="256" placeholder="精确匹配" @keyup.enter="refresh" /></label>
      <label>技术关联标识<input v-model="traceId" maxlength="64" placeholder="用于关联请求和日志" @keyup.enter="refresh" /></label>
      <label>开始时间<input v-model="startedAt" type="datetime-local" @change="refresh" /></label>
      <label>结束时间<input v-model="endedAt" type="datetime-local" @change="refresh" /></label>
      <button class="secondary-btn" :disabled="pager.state.loading" @click="refresh">应用筛选</button>
      </section>
    </details>
    <section class="surface">
      <ResourceState :loading="pager.state.loading" :error="pager.state.error" :empty="!pager.state.items.length" empty-text="暂无治理审计记录" @retry="refresh">
        <div class="table-wrap">
          <table>
            <thead><tr><th>时间</th><th>操作</th><th>操作人</th><th>资源</th><th>结果与风险</th><th>详情</th></tr></thead>
            <tbody>
              <tr v-for="item in pager.state.items" :key="item.audit_id">
                <td>{{ formatDate(item.timestamp) }}</td>
                <td>{{ displayLabel(item.operation, 'operation') }}</td>
                <td>{{ item.actor }}</td>
                <td><span>{{ displayLabel(item.resource_type, 'resource_type') }}</span><small>{{ item.resource_id }}</small></td>
                <td>
                  <span>{{ item.reason || '已记录治理操作' }}</span>
                  <small>{{ item.resource_version ? `版本 ${item.resource_version}` : '历史版本未采集' }}</small>
                </td>
                <td>
                  <details class="row-details">
                    <summary>查看技术详情</summary>
                    <div>{{ item.reason || '未提供原因' }}</div>
                    <span v-if="!item.trace_id">历史记录未采集 Trace</span>
                    <span v-else class="trace-value"><code>{{ item.trace_id }}</code><button class="icon-btn" title="复制 Trace ID" @click="copyTrace(item.trace_id)"><Check v-if="copiedTrace === item.trace_id" :size="14" /><Copy v-else :size="14" /></button></span>
                  </details>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <PaginationBar :count="pager.state.items.length" :can-previous="pager.state.history.length > 0" :can-next="Boolean(pager.state.nextCursor)" @previous="pager.previous" @next="pager.next" />
      </ResourceState>
    </section>
  </div>
</template>

<style scoped>
.audit-filters { align-items: flex-end; flex-wrap: wrap; }
.audit-filters label { flex: 1 1 180px; }
.trace-value { display: inline-flex; align-items: center; gap: 6px; }
.trace-value .icon-btn { width: 28px; height: 28px; flex: 0 0 auto; }
</style>
