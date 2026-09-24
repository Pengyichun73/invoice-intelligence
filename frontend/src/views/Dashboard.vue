<script setup>
import { computed, onMounted, reactive } from 'vue'
import {
  AlertTriangle,
  ArrowRight,
  BookOpenCheck,
  CheckCircle2,
  FileSearch,
  GitCompareArrows,
  RefreshCw,
  ScanSearch,
  ShieldCheck,
  Sparkles,
} from 'lucide-vue-next'
import StatusBadge from '../components/StatusBadge.vue'
import { governanceApi } from '../api/governance'
import { invoiceApi } from '../api/invoice'

const emit = defineEmits(['navigate'])
const state = reactive({
  loading: false,
  error: '',
  online: false,
  admissions: { pending: null, quarantined: null, pendingMore: false, quarantinedMore: false },
  examples: null,
  examplesMore: false,
  conflicts: null,
  conflictsMore: false,
  completed: null,
  completedMore: false,
  ocr: null,
  refreshedAt: null,
})

const today = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: 'long',
  day: 'numeric',
  weekday: 'short',
}).format(new Date())

const pendingTotal = computed(() => {
  const values = [state.admissions.pending, state.admissions.quarantined]
  return values.some((value) => value === null)
    ? null
    : values.reduce((total, value) => total + value, 0)
})
const governanceUnknown = computed(() => (
  state.admissions.pending === null
  || state.admissions.quarantined === null
  || state.conflicts === null
))

const readiness = computed(() => {
  if (!state.online) return { label: '服务未连接', tone: 'offline', detail: '上传和治理操作暂不可用' }
  if ([state.admissions.pending, state.admissions.quarantined, state.examples, state.conflicts, state.completed].some((item) => item === null)) {
    return { label: '部分数据未读取', tone: 'attention', detail: '不会以 0 代替缺失状态' }
  }
  if (pendingTotal.value) return { label: '有待处理事项', tone: 'attention', detail: '建议先处理字段冲突' }
  return { label: '运行正常', tone: 'online', detail: '当前没有治理待办' }
})

const ocrSummary = computed(() => {
  if (!state.ocr) return { label: '未读取', detail: '尚未取得多源识别指标', tone: 'neutral' }
  if (state.ocr.unavailable_count) return { label: '部分不可用', detail: String(state.ocr.unavailable_count) + ' 次服务不可用', tone: 'attention' }
  if (state.ocr.provider_call_count) return { label: '多源可用', detail: '已处理 ' + state.ocr.page_call_count + ' 页', tone: 'online' }
  return { label: '等待调用', detail: '尚无识别调用记录', tone: 'neutral' }
})

const priorityTasks = computed(() => {
  const tasks = []
  if (state.conflicts) {
    tasks.push({
      key: 'conflicts',
      icon: GitCompareArrows,
      title: shown(state.conflicts, state.conflictsMore) + ' 条字段映射冲突',
      description: '同一标签存在多个标准字段候选，需要人工裁决。',
      status: 'review_required',
      action: '处理冲突',
    })
  }
  if (state.admissions.quarantined) {
    tasks.push({
      key: 'admissions',
      icon: AlertTriangle,
      title: shown(state.admissions.quarantined, state.admissions.quarantinedMore) + ' 条记忆已隔离',
      description: '存在高风险、证据不足或质量冲突，需要再次核对。',
      status: 'quarantined',
      action: '核对隔离项',
    })
  }
  if (state.admissions.pending) {
    tasks.push({
      key: 'admissions',
      icon: ShieldCheck,
      title: shown(state.admissions.pending, state.admissions.pendingMore) + ' 条案例等待准入',
      description: '审核事实已保存，但尚未进入长期检索。',
      status: 'pending',
      action: '处理准入',
    })
  }
  return tasks
})

function count(payload) {
  return Number(payload?.items?.length || 0)
}

function shown(value, more = false) {
  return value === null ? '未读取' : String(value) + (more ? '+' : '')
}

function todayRange() {
  const start = new Date()
  start.setHours(0, 0, 0, 0)
  const end = new Date(start)
  end.setDate(end.getDate() + 1)
  return { started_at: start.toISOString(), ended_at: end.toISOString() }
}

async function load() {
  state.loading = true
  state.error = ''
  const range = todayRange()
  const results = await Promise.allSettled([
    invoiceApi.health(),
    governanceApi.admissions({ status: 'pending', limit: 100 }),
    governanceApi.admissions({ status: 'quarantined', limit: 100 }),
    governanceApi.examples({ is_valid: true, limit: 100 }),
    governanceApi.conflicts({ status: 'open', limit: 100 }),
    governanceApi.audits({ ...range, limit: 100 }),
    governanceApi.ocrMetrics(),
  ])
  const [health, pending, quarantined, examples, conflicts, completed, ocr] = results
  state.online = health.status === 'fulfilled'
  state.admissions.pending = pending.status === 'fulfilled' ? count(pending.value) : null
  state.admissions.quarantined = quarantined.status === 'fulfilled' ? count(quarantined.value) : null
  state.examples = examples.status === 'fulfilled' ? count(examples.value) : null
  state.conflicts = conflicts.status === 'fulfilled' ? count(conflicts.value) : null
  state.completed = completed.status === 'fulfilled' ? count(completed.value) : null
  state.admissions.pendingMore = pending.status === 'fulfilled' && Boolean(pending.value?.next_cursor)
  state.admissions.quarantinedMore = quarantined.status === 'fulfilled' && Boolean(quarantined.value?.next_cursor)
  state.examplesMore = examples.status === 'fulfilled' && Boolean(examples.value?.next_cursor)
  state.conflictsMore = conflicts.status === 'fulfilled' && Boolean(conflicts.value?.next_cursor)
  state.completedMore = completed.status === 'fulfilled' && Boolean(completed.value?.next_cursor)
  state.ocr = ocr.status === 'fulfilled' ? ocr.value : null
  if (!state.online) state.error = '后端暂时不可用，上传和治理操作需等待连接恢复。'
  else if (results.slice(1).some((item) => item.status === 'rejected')) {
    state.error = '部分治理数据暂时未读取，页面不会用 0 代替真实状态。'
  }
  state.refreshedAt = new Date()
  state.loading = false
}

function go(key) {
  emit('navigate', key)
}

onMounted(load)
</script>

<template>
  <div class="dashboard-page">
    <section class="task-hero">
      <div class="task-hero-copy">
        <span class="hero-date">{{ today }} · 工作概览</span>
        <h1>先处理需要人工判断的事项</h1>
        <p>系统已按证据完整度和风险排序。当前图片证据始终优先，历史案例不会替你决定本次结果。</p>
        <div class="hero-actions">
          <button class="primary-btn hero-primary" @click="go('extraction')"><FileSearch :size="17" />上传并提取发票</button>
          <button class="text-btn" @click="go('admissions')">查看全部治理任务 <ArrowRight :size="15" /></button>
        </div>
      </div>
      <div class="hero-assurance" aria-label="可信处理原则">
        <Sparkles :size="18" />
        <div><strong>证据不足，不自动填充</strong><small>识别冲突和字段绑定不确定时进入人工审核。</small></div>
      </div>
    </section>

    <div v-if="state.error" class="alert neutral"><AlertTriangle :size="17" /><span>{{ state.error }}</span></div>

    <section class="task-metrics" aria-label="工作概览">
      <button class="task-metric attention" @click="go('admissions')">
        <span>待处理</span>
        <strong>{{ shown(pendingTotal) }}</strong>
        <small>待准入与已隔离案例</small>
      </button>
      <button class="task-metric" @click="go('audits')">
        <span>今日完成</span>
        <strong>{{ shown(state.completed, state.completedMore) }}</strong>
        <small>今日已记录的治理动作</small>
      </button>
      <button class="task-metric" @click="go('examples')">
        <span>有效案例</span>
        <strong>{{ shown(state.examples, state.examplesMore) }}</strong>
        <small>已审核、已批准且有效</small>
      </button>
      <button class="task-metric attention" @click="go('conflicts')">
        <span>开放冲突</span>
        <strong>{{ shown(state.conflicts, state.conflictsMore) }}</strong>
        <small>字段语义与证据绑定冲突</small>
      </button>
    </section>

    <section class="dashboard-workspace">
      <div class="surface priority-panel">
        <header class="section-head">
          <div><span>按风险排序</span><h2>需要你处理</h2></div>
          <button class="icon-text-btn" :disabled="state.loading" @click="load">
            <RefreshCw :class="{ spin: state.loading }" :size="15" />刷新
          </button>
        </header>
        <div v-if="priorityTasks.length" class="priority-list">
          <button v-for="task in priorityTasks" :key="task.key + '-' + task.status" @click="go(task.key)">
            <span class="priority-icon"><component :is="task.icon" :size="18" /></span>
            <span class="priority-copy"><strong>{{ task.title }}</strong><small>{{ task.description }}</small></span>
            <StatusBadge :value="task.status" />
            <span class="priority-action">{{ task.action }} <ArrowRight :size="14" /></span>
          </button>
        </div>
        <div v-else-if="state.loading" class="dashboard-loading"><span class="skeleton-line" /><span class="skeleton-line short" /><span class="skeleton-line" /></div>
        <div v-else-if="governanceUnknown" class="empty-success unknown"><AlertTriangle :size="24" /><div><strong>待办状态尚未读取</strong><small>后端连接恢复后可重新刷新，不会以零代替未知状态。</small></div></div>
        <div v-else class="empty-success"><CheckCircle2 :size="24" /><div><strong>当前没有待处理事项</strong><small>新的审核或治理任务出现后会显示在这里。</small></div></div>
      </div>

      <aside class="surface system-panel">
        <header class="section-head"><div><span>真实运行状态</span><h2>处理链路</h2></div><ScanSearch :size="18" /></header>
        <div class="system-list">
          <div><i :class="{ online: state.online }" /><span><strong>文件与处理流程</strong><small>{{ state.online ? '后端连接正常' : '服务未连接' }}</small></span></div>
          <div><i :class="{ online: Boolean(state.ocr) }" /><span><strong>视觉与文字识别</strong><small>{{ ocrSummary.detail }}</small></span></div>
          <div><i :class="{ online: state.examples !== null }" /><span><strong>可信记忆</strong><small>仅已批准案例可参与检索</small></span></div>
          <div><i :class="{ online: state.conflicts !== null }" /><span><strong>字段语义治理</strong><small>冲突不会被自动覆盖</small></span></div>
        </div>
        <div class="system-note"><BookOpenCheck :size="16" /><span>人工提交表示审核事实，仍需通过准入门禁才能成为长期记忆。</span></div>
        <footer>
          <span v-if="state.refreshedAt">{{ state.refreshedAt.toLocaleTimeString('zh-CN', { hour12: false }) }} 更新</span>
          <button class="text-btn" @click="go('audits')">查看审计记录 <ArrowRight :size="14" /></button>
        </footer>
      </aside>
    </section>
  </div>
</template>
