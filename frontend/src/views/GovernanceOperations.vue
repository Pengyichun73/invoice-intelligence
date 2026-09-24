<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { Activity, CheckCircle2, FileSearch, RefreshCw, RotateCcw, ShieldAlert, XCircle } from 'lucide-vue-next'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { displayLabel, errorFeedback, formatDate } from '../api/client'
import { governanceApi } from '../api/governance'
import { useNotifications } from '../composables/useNotifications'

const notices = useNotifications()
const mode = ref('evaluation')
const state = reactive({ loading: false, error: '', jobs: [], selectedJob: null })
const training = reactive({ id: '', item: null, reason: '', busy: false, error: '' })
const promotion = reactive({ candidateId: '', evaluationRunId: '', artifactId: '', expectedRevision: 1, targetStatus: 'shadow', reason: '', targetCandidateId: '', item: null, busy: false, error: '' })
const transaction = reactive({ runId: '', item: null, decision: 'confirmed', expectedRevision: 1, busy: false, error: '' })

const modeLabel = computed(() => ({ evaluation: '离线评估任务', training: '训练任务', promotion: '模型晋升', transaction: '交易分析' }[mode.value]))

function showError(target, error) {
  const feedback = errorFeedback(error)
  target.error = feedback.message
  notices.error('治理操作未完成', feedback.message)
}

async function loadEvaluations() {
  state.loading = true
  state.error = ''
  try {
    state.jobs = await governanceApi.evaluationJobs({ limit: 50, offset: 0 })
  } catch (error) {
    state.error = errorFeedback(error).message
  } finally {
    state.loading = false
  }
}

async function loadEvaluation(job) {
  state.selectedJob = await governanceApi.evaluationJob(job.job_id)
}

async function loadTraining() {
  if (!training.id.trim()) return
  training.busy = true
  training.error = ''
  try {
    training.item = await governanceApi.trainingJob(training.id.trim())
  } catch (error) {
    showError(training, error)
  } finally {
    training.busy = false
  }
}

async function cancelTraining() {
  training.busy = true
  try {
    training.item = await governanceApi.cancelTrainingJob(training.item.job_id, training.reason.trim() || 'operator_requested')
    notices.success('训练取消已登记', '后端状态已更新，实际远程取消由训练 Worker 执行。')
  } catch (error) { showError(training, error) } finally { training.busy = false }
}

async function retryTraining() {
  training.busy = true
  try {
    training.item = await governanceApi.retryTrainingJob(training.item.job_id)
    notices.success('训练重试已登记', '新任务等待独立 Worker 领取。')
  } catch (error) { showError(training, error) } finally { training.busy = false }
}

async function createPromotion() {
  promotion.busy = true
  promotion.error = ''
  try {
    promotion.item = await governanceApi.createPromotionCandidate({
      candidate_id: promotion.candidateId.trim(),
      evaluation_run_id: promotion.evaluationRunId.trim(),
      artifact_id: promotion.artifactId.trim(),
    })
    notices.success('晋升候选已登记', '门禁事实由后端 Evaluation、Artifact 和版本 Registry 校验。')
  } catch (error) { showError(promotion, error) } finally { promotion.busy = false }
}

async function approvePromotion() {
  promotion.busy = true
  try {
    promotion.item = await governanceApi.approvePromotionCandidate(promotion.item.candidate_id, {
      expected_revision: Number(promotion.expectedRevision),
      target_status: promotion.targetStatus,
    })
    notices.success('晋升决定已提交', '后端已记录审核事实；不会在请求内修改模型权重。')
  } catch (error) { showError(promotion, error) } finally { promotion.busy = false }
}

async function rejectPromotion() {
  promotion.busy = true
  try {
    promotion.item = await governanceApi.rejectPromotionCandidate(promotion.item.candidate_id, {
      expected_revision: Number(promotion.expectedRevision),
      reason: promotion.reason.trim() || 'operator_rejected',
    })
    notices.success('晋升候选已拒绝', '拒绝事实已写入审计。')
  } catch (error) { showError(promotion, error) } finally { promotion.busy = false }
}

async function rollbackPromotion() {
  promotion.busy = true
  try {
    promotion.item = await governanceApi.rollbackPromotionCandidate(promotion.item.candidate_id, {
      expected_revision: Number(promotion.expectedRevision),
      target_candidate_id: promotion.targetCandidateId.trim(),
    })
    notices.success('回滚请求已提交', '后端只允许切换到已验证且仍有效的历史候选。')
  } catch (error) { showError(promotion, error) } finally { promotion.busy = false }
}

async function analyzeTransaction() {
  transaction.busy = true
  transaction.error = ''
  try {
    transaction.item = await governanceApi.analyzeTransaction(transaction.runId.trim())
    transaction.expectedRevision = transaction.item.revision
    notices.success('交易候选已生成', '来源必须是同租户已完成且已持久化的提取结果。')
  } catch (error) { showError(transaction, error) } finally { transaction.busy = false }
}

async function reviewTransaction() {
  transaction.busy = true
  try {
    transaction.item = await governanceApi.reviewTransaction(transaction.item.candidate_transaction_id, {
      decision: transaction.decision,
      expected_revision: Number(transaction.expectedRevision),
    })
    transaction.expectedRevision = transaction.item.revision
    notices.success('交易复核已提交', '复核事实已记录；模型分数仍仅作为 advisory。')
  } catch (error) { showError(transaction, error) } finally { transaction.busy = false }
}

onMounted(loadEvaluations)
</script>

<template>
  <div>
    <PageHeader eyebrow="运行治理" title="后台治理操作" description="仅调用受控 API；租户与审核人来自可信上下文，不在前端表单中接收覆盖字段。">
      <button class="secondary-btn" :disabled="state.loading" @click="loadEvaluations"><RefreshCw :size="16" />刷新任务</button>
    </PageHeader>

    <div class="segmented-control index-mode">
      <button :class="{ active: mode === 'evaluation' }" @click="mode = 'evaluation'"><Activity :size="16" />评估任务</button>
      <button :class="{ active: mode === 'training' }" @click="mode = 'training'"><RotateCcw :size="16" />训练任务</button>
      <button :class="{ active: mode === 'promotion' }" @click="mode = 'promotion'"><CheckCircle2 :size="16" />模型晋升</button>
      <button :class="{ active: mode === 'transaction' }" @click="mode = 'transaction'"><FileSearch :size="16" />交易分析</button>
    </div>

    <section v-if="mode === 'evaluation'" class="surface">
      <header class="section-head"><div><span>Evaluation Job</span><h2>{{ state.jobs.length }} 个任务</h2></div><StatusBadge :value="state.jobs.some((item) => ['running', 'planned'].includes(item.status)) ? 'running' : 'completed'" /></header>
      <p class="context-note">诊断型任务仅汇总预先提交的判断值，不能作为晋升证据。Suite 任务已绑定冻结数据集与版本；缺少真实 Runner 时会被隔离，只有真实完成的 Run 和报告产物才可进入晋升门禁。</p>
      <ResourceState :loading="state.loading" :error="state.error" :empty="!state.jobs.length" empty-text="当前租户没有评估任务" @retry="loadEvaluations">
        <div class="record-list"><article v-for="job in state.jobs" :key="job.job_id" @click="loadEvaluation(job)"><header><code>{{ job.job_id }}</code><StatusBadge :value="job.status" /></header><p>{{ job.evidence_class === 'suite_run' ? `Suite ${job.suite || '未知'}` : '诊断型' }} · {{ job.dataset_version }} · {{ job.model_version }} · {{ job.prompt_version }}</p><footer>尝试 {{ job.attempt_count }} 次 · {{ formatDate(job.updated_at) }}<span v-if="job.failure_code">不可用原因 {{ job.failure_code }}</span></footer></article></div>
      </ResourceState>
      <article v-if="state.selectedJob" class="detail-surface"><header class="section-head"><div><span>评估详情</span><h2>{{ state.selectedJob.job_id }}</h2></div><StatusBadge :value="state.selectedJob.status" /></header><dl class="meta-list columns"><div><dt>证据类型</dt><dd>{{ state.selectedJob.evidence_class === 'suite_run' ? (state.selectedJob.evaluation_run_id ? 'Suite Run 已确认' : 'Suite 任务登记') : '诊断结果' }}</dd></div><div><dt>Suite</dt><dd>{{ state.selectedJob.suite || '不适用' }}</dd></div><div><dt>Evaluation Run</dt><dd>{{ state.selectedJob.evaluation_run_id || '尚未确认' }}</dd></div><div><dt>数据集版本</dt><dd>{{ state.selectedJob.dataset_version }}</dd></div><div><dt>索引版本</dt><dd>{{ state.selectedJob.index_version }}</dd></div><div><dt>模型版本</dt><dd>{{ state.selectedJob.model_version }}</dd></div><div><dt>提示词版本</dt><dd>{{ state.selectedJob.prompt_version }}</dd></div><div><dt>阈值版本</dt><dd>{{ state.selectedJob.threshold_version }}</dd></div><div><dt>不可用原因</dt><dd>{{ state.selectedJob.failure_code || '无' }}</dd></div></dl></article>
    </section>

    <section v-else-if="mode === 'training'" class="surface">
      <header class="section-head"><div><span>Training Job</span><h2>状态查询与控制</h2></div><ShieldAlert :size="20" /></header>
      <div class="toolbar lookup-wide"><label>任务 ID<input v-model="training.id" placeholder="输入训练任务 ID" @keyup.enter="loadTraining" /></label><button class="secondary-btn" :disabled="training.busy" @click="loadTraining"><RefreshCw :size="16" />查询</button></div>
      <p v-if="training.error" class="alert error">{{ training.error }}</p>
      <article v-if="training.item" class="record-list"><header><strong>{{ training.item.job_id }}</strong><StatusBadge :value="training.item.status" /></header><dl class="meta-list columns"><div><dt>数据集版本</dt><dd>{{ training.item.dataset_version }}</dd></div><div><dt>模型版本</dt><dd>{{ training.item.model_version }}</dd></div><div><dt>训练运行</dt><dd>{{ training.item.training_run_id }}</dd></div><div><dt>revision</dt><dd>{{ training.item.revision }}</dd></div><div><dt>失败码</dt><dd>{{ training.item.failure_code || '无' }}</dd></div><div><dt>更新时间</dt><dd>{{ formatDate(training.item.updated_at) }}</dd></div></dl><div class="form-grid"><label>取消原因<input v-model="training.reason" placeholder="operator_requested" /></label></div><div class="button-row"><button class="secondary-btn" :disabled="training.busy" @click="cancelTraining">取消任务</button><button class="primary-btn" :disabled="training.busy" @click="retryTraining">登记重试</button></div></article>
    </section>

    <section v-else-if="mode === 'promotion'" class="surface">
      <header class="section-head"><div><span>Promotion Candidate</span><h2>可信门禁与人工审批</h2></div><ShieldAlert :size="20" /></header>
      <div class="form-grid"><label>候选 ID<input v-model="promotion.candidateId" /></label><label>Evaluation Run ID<input v-model="promotion.evaluationRunId" /></label><label>Artifact ID<input v-model="promotion.artifactId" /></label></div>
      <button class="primary-btn" :disabled="promotion.busy" @click="createPromotion">创建候选</button>
      <p class="context-note">客户端不提交指标、hard failure 或兼容性诊断；缺少可信证据时后端 fail closed。</p>
      <article v-if="promotion.item" class="record-list"><header><strong>{{ promotion.item.candidate_id }}</strong><StatusBadge :value="promotion.item.status" /></header><p>评估 {{ promotion.item.evaluation_run_id }} · Artifact {{ promotion.item.artifact_id || '无' }}</p><footer>revision {{ promotion.item.revision }} · 硬失败 {{ promotion.item.hard_failure_code || '无' }} · 兼容性错误 {{ promotion.item.compatibility_errors.length }}</footer><div class="form-grid"><label>目标状态<select v-model="promotion.targetStatus"><option value="shadow">shadow</option><option value="canary">canary</option><option value="active">active</option></select></label><label>期望 revision<input v-model.number="promotion.expectedRevision" type="number" min="1" /></label><label>拒绝原因<input v-model="promotion.reason" placeholder="operator_rejected" /></label><label>回滚目标候选 ID<input v-model="promotion.targetCandidateId" /></label></div><div class="button-row"><button class="primary-btn" :disabled="promotion.busy" @click="approvePromotion">审批</button><button class="secondary-btn" :disabled="promotion.busy" @click="rejectPromotion">拒绝</button><button class="secondary-btn" :disabled="promotion.busy" @click="rollbackPromotion">回滚</button></div></article>
    </section>

    <section v-else class="surface">
      <header class="section-head"><div><span>Transaction Analysis</span><h2>候选分析与复核</h2></div><FileSearch :size="20" /></header>
      <div class="toolbar lookup-wide"><label>已完成提取 Run ID<input v-model="transaction.runId" placeholder="输入 run_id" @keyup.enter="analyzeTransaction" /></label><button class="primary-btn" :disabled="transaction.busy" @click="analyzeTransaction">分析候选</button></div>
      <p class="context-note">仅允许同租户已完成 Run；不在请求中接收 tenant_id 或 reviewer_id。</p>
      <article v-if="transaction.item" class="record-list"><header><strong>{{ transaction.item.candidate_transaction_id }}</strong><StatusBadge :value="transaction.item.status" /></header><p>评估状态：{{ displayLabel(transaction.item.status) }} · revision {{ transaction.item.revision }}</p><footer>审核人由可信上下文提供 · advisory score 不表示概率</footer><div class="form-grid"><label>复核决定<select v-model="transaction.decision"><option value="confirmed">confirmed</option><option value="dismissed">dismissed</option><option value="escalated">escalated</option></select></label><label>期望 revision<input v-model.number="transaction.expectedRevision" type="number" min="1" /></label></div><button class="primary-btn" :disabled="transaction.busy" @click="reviewTransaction">提交复核</button></article>
    </section>
  </div>
</template>
