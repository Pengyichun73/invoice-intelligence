<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { BarChart3, RefreshCw, Search, ShieldCheck } from 'lucide-vue-next'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { formatDate, INVOICE_FIELDS } from '../api/client.js'
import { governanceApi } from '../api/governance.js'
import { canShowBenefitMetrics } from './effectivenessPresentation.js'

const state = reactive({ overview: null, stages: null, scenarios: null, run: null, loading: false, error: '', gold: null, goldMessage: '', goldStatus: '', job: null })
const runId = ref('')
const fieldPath = ref('')
const documentId = ref('')
const jobDocuments = ref('')
const templateGroup = ref('')
const versions = reactive({ schema_version: '3.0.0', model_version: '', prompt_version: '', catalog_version: '', index_version: '' })
const annotation = reactive(Object.fromEntries(Object.keys(INVOICE_FIELDS).map((path) => [path, { state: '', value: '', page_number: '', bounding_box: '', observed_text: '' }])))
const goldReady = computed(() => import.meta.env.VITE_MEMORY_GOLD_ENABLED === 'true' && state.overview && !state.error)
const benefitStatus = computed(() => state.scenarios?.benefit_status || state.overview?.benefit_status || 'insufficient_evidence')
const showBenefitMetrics = computed(() => canShowBenefitMetrics(benefitStatus.value, state.run, state.scenarios))
const blockers = computed(() => state.scenarios?.blocker_codes || state.overview?.benefit_blocker_codes || [])
const scenarios = computed(() => showBenefitMetrics.value ? state.scenarios.scenarios : [])
const groupedScenarios = computed(() => scenarios.value.reduce((groups, item) => {
  ;(groups[item.dimension] ||= []).push(item)
  return groups
}, {}))
const stageLabels = { documents: '文档', extraction: '提取', review: '人工审核', admission: '记忆准入', projection: '索引投影', retrieval: '案例召回', evaluation_jobs: '评估任务', frozen_gold: '冻结真值', targeted_reread: '定向重读', paired_benefit: '配对收益' }
const dimensionLabels = { field_path: '字段', template_group: '模板组', image_quality: '图片质量', ocr_status: 'OCR 状态', error_category: '错误类别' }

function failure(error) {
  if (error.status === 403) return '当前账号没有评估查询权限。'
  if (error.status === 503) return '后端依赖尚未配置或暂不可用。'
  return error.message || '查询失败'
}
async function refresh() {
  state.loading = true
  state.error = ''
  try {
    const selectedRunId = runId.value || undefined
    const [overview, stages, scenarios] = await Promise.all([
      governanceApi.effectivenessOverview(),
      governanceApi.effectivenessStages(),
      governanceApi.effectivenessScenarios({ run_id: selectedRunId, field_path: fieldPath.value || undefined }),
    ])
    state.overview = overview
    state.stages = stages
    state.scenarios = scenarios
    if (!runId.value && overview.latest_benefit_run_id) runId.value = overview.latest_benefit_run_id
    if (runId.value) state.run = await governanceApi.effectivenessRun(runId.value)
  } catch (error) { state.error = failure(error) }
  finally { state.loading = false }
}
async function queryRun() {
  if (!runId.value.trim()) return
  state.loading = true
  state.error = ''
  try {
    const id = runId.value.trim()
    const [run, scenarios] = await Promise.all([
      governanceApi.effectivenessRun(id),
      governanceApi.effectivenessScenarios({ run_id: id, field_path: fieldPath.value || undefined }),
    ])
    state.run = run
    state.scenarios = scenarios
  } catch (error) { state.error = failure(error) }
  finally { state.loading = false }
}
async function queryGold() {
  if (!documentId.value.trim()) return
  state.goldMessage = ''
  state.gold = null
  state.goldStatus = ''
  try { state.gold = await governanceApi.goldCase(documentId.value.trim()) }
  catch (error) {
    state.goldStatus = error.status === 404 ? '当前文档尚无标注事实。' : failure(error)
  }
}
function annotationBody() {
  if (!templateGroup.value.trim() || Object.values(versions).some((value) => !value.trim())) throw new Error('请填写模板组和全部版本。')
  const fields = {}
  for (const [path, item] of Object.entries(annotation)) {
    if (!item.state) throw new Error(`请确认 ${INVOICE_FIELDS[path]} 的状态。`)
    if (item.state !== 'present') { fields[path] = { state: item.state, value: null }; continue }
    const box = item.bounding_box.split(',').map((part) => Number(part.trim()))
    if (!item.value.trim() || !Number.isInteger(Number(item.page_number)) || Number(item.page_number) < 1 || box.length !== 4 || box.some((number) => !Number.isFinite(number)) || box[2] <= box[0] || box[3] <= box[1] || !item.observed_text.trim()) {
      throw new Error(`${INVOICE_FIELDS[path]} 缺少当前图片的值、页码、有效坐标或原文。`)
    }
    fields[path] = { state: 'present', value: item.value.trim(), page_number: Number(item.page_number), bounding_box: box, observed_text: item.observed_text.trim() }
  }
  return { template_group: templateGroup.value.trim(), versions: { ...versions }, fields }
}
async function submitAnnotation() {
  state.goldMessage = ''
  if (!documentId.value.trim()) { state.goldMessage = '请填写现有文档编号。'; return }
  try {
    const body = annotationBody()
    state.gold = await governanceApi.submitGoldAnnotation(documentId.value.trim(), body)
    state.goldMessage = '标注已保存。仍需另一位独立标注人和第三人裁决后才能冻结。'
  } catch (error) { state.goldMessage = failure(error) }
}
async function createJob() {
  state.goldMessage = ''
  const ids = jobDocuments.value.split(/[\s,，]+/).map((item) => item.trim()).filter(Boolean)
  if (!ids.length || new Set(ids).size !== ids.length || ids.length > 200) { state.goldMessage = '请输入 1 至 200 个互不重复的已冻结文档编号。'; return }
  try {
    state.job = await governanceApi.createMemoryBenefitJob(ids)
    state.goldMessage = `配对评估任务已登记：${state.job.job_id}。结果须待 Worker 完成后查询。`
  } catch (error) { state.goldMessage = failure(error) }
}
async function queryJob() {
  if (!state.job?.job_id) return
  try { state.job = await governanceApi.evaluationJob(state.job.job_id) }
  catch (error) { state.goldMessage = failure(error) }
}
onMounted(refresh)
</script>

<template>
  <div>
    <PageHeader eyebrow="可信记忆" title="记忆效果" description="运行事实与配对收益分别呈现。" />
    <section class="toolbar lookup-wide"><button class="secondary-btn" :disabled="state.loading" @click="refresh"><RefreshCw :size="16" />刷新状态</button></section>
    <ResourceState :loading="state.loading" :error="state.error" :empty="!state.overview" empty-text="尚未读取效果状态" @retry="refresh">
      <template v-if="state.overview">
        <section class="surface">
          <header class="section-head"><div><span>运行事实</span><h2>处理链路</h2></div><span>{{ formatDate(state.overview.since) }} 至 {{ formatDate(state.overview.until) }}</span></header>
          <div class="effect-stage-grid"><div v-for="stage in state.stages?.stages || state.overview.stages" :key="stage.name" class="effect-stage"><strong>{{ stageLabels[stage.name] || stage.name }}</strong><StatusBadge :value="stage.status" /><span>{{ stage.observed_count }} 条 · {{ stage.count_scope === 'current' ? '当前' : '窗口' }}</span><small>{{ stage.version || '无版本' }} · {{ formatDate(stage.evidence_at) }}</small><small v-if="stage.blocker_codes.length">{{ stage.blocker_codes.join('、') }}</small></div></div>
          <p class="context-note">召回与索引计数只表示链路运行，不代表识别准确率改善。</p>
        </section>
        <section class="surface">
          <header class="section-head"><div><span>独立配对评估</span><h2>记忆收益</h2></div><StatusBadge :value="benefitStatus" /></header>
          <div class="effect-metrics"><div><span>冻结真值</span><strong>{{ state.overview.operational.frozen_gold_case_count }}</strong></div><div><span>模板组</span><strong>{{ state.overview.operational.frozen_template_group_count }}</strong></div><div><span>最新批次样本</span><strong>{{ state.run?.case_count ?? '未报告' }}</strong></div></div>
          <p v-if="benefitStatus === 'insufficient_evidence'" class="context-note">配对证据不足；当前不能得出改善百分比结论。阻塞码：{{ blockers.join('、') || '未报告' }}</p>
          <p v-else-if="blockers.length" class="context-note">未通过门禁：{{ blockers.join('、') }}</p>
          <div class="lookup"><label>Benefit run_id<input v-model="runId" placeholder="输入配对评估运行编号" @keyup.enter="queryRun" /></label><button class="icon-btn" title="查询配对运行" @click="queryRun"><Search :size="17" /></button></div>
          <dl v-if="state.run" class="meta-list columns"><div><dt>任务状态</dt><dd><StatusBadge :value="state.run.status" /></dd></div><div><dt>样本 / 模板组</dt><dd>{{ state.run.case_count }} / {{ state.run.template_group_count }}</dd></div><div><dt>索引版本</dt><dd>{{ state.run.index_version }}</dd></div><div><dt>模型 / Prompt</dt><dd>{{ state.run.model_version }} / {{ state.run.prompt_version }}</dd></div></dl>
          <template v-if="showBenefitMetrics">
            <div class="effect-metrics"><div><span>正确字段差值</span><strong>{{ state.run.metrics.correct_field_delta }}</strong></div><div><span>待审下降</span><strong>{{ (state.run.metrics.review_reduction_vs_ocr * 100).toFixed(1) }}%</strong></div><div><span>错误自动通过</span><strong>{{ state.run.metrics.wrong_auto_passes }}</strong></div><div><span>p95 额外耗时</span><strong>{{ (state.run.metrics.p95_extra_ms / 1000).toFixed(1) }} 秒</strong></div></div>
          </template>
          <div class="comparison-controls"><label>字段筛选<select v-model="fieldPath" @change="runId ? queryRun() : refresh()"><option value="">全部场景</option><option v-for="(label, path) in INVOICE_FIELDS" :key="path" :value="path">{{ label }}</option></select></label></div>
          <template v-if="showBenefitMetrics">
            <div v-for="(items, dimension) in groupedScenarios" :key="dimension"><h3 class="subheading">{{ dimensionLabels[dimension] || dimension }}</h3><div class="table-wrap"><table><thead><tr><th>场景</th><th>样本</th><th>正确字段差值</th><th>待审下降</th><th>错误自动通过</th><th>p95 额外耗时</th><th>门禁</th></tr></thead><tbody><tr v-for="item in items" :key="`${item.value}-${item.field_path}`"><td>{{ item.value }}</td><td>{{ item.sample_count }}</td><td>{{ item.correct_field_delta }}</td><td>{{ (item.review_reduction_vs_ocr * 100).toFixed(1) }}%</td><td>{{ item.wrong_auto_passes }}</td><td>{{ (item.p95_extra_ms / 1000).toFixed(1) }} 秒</td><td><StatusBadge :value="item.passed ? 'passed' : 'warning'" /></td></tr></tbody></table></div></div>
          </template>
        </section>
        <section v-if="goldReady" class="surface">
          <header class="section-head"><div><span>受控样本</span><h2>标注与配对任务</h2></div><ShieldCheck :size="18" /></header>
          <div class="lookup"><label>现有文档编号<input v-model="documentId" placeholder="document_id" @keyup.enter="queryGold" /></label><button class="icon-btn" title="查询真值状态" @click="queryGold"><Search :size="17" /></button></div>
          <p v-if="state.goldStatus" class="context-note">{{ state.goldStatus }}</p>
          <dl v-if="state.gold" class="meta-list columns"><div><dt>状态</dt><dd>{{ state.gold.status }}</dd></div><div><dt>独立标注</dt><dd>{{ state.gold.annotation_count }} / 2</dd></div><div><dt>模板组</dt><dd>{{ state.gold.template_group }}</dd></div><div><dt>冻结时间</dt><dd>{{ formatDate(state.gold.frozen_at) }}</dd></div></dl>
          <details v-if="!state.gold || state.gold.status !== 'frozen'" class="technical-details"><summary>提交独立标注</summary>
            <p class="context-note">逐字段依据当前图片填写；不会从识别结果自动生成真值。</p>
            <div class="effect-form-grid"><label>模板组<input v-model="templateGroup" /></label><label v-for="(_, key) in versions" :key="key">{{ key }}<input v-model="versions[key]" /></label></div>
            <div class="effect-annotation-list"><div v-for="(label, path) in INVOICE_FIELDS" :key="path" class="effect-annotation-row"><strong>{{ label }} <code>{{ path }}</code></strong><select v-model="annotation[path].state"><option value="">选择状态</option><option value="present">当前图片可读</option><option value="absent">图片中不存在</option><option value="unreadable">无法辨认</option></select><template v-if="annotation[path].state === 'present'"><input v-model="annotation[path].value" placeholder="字段值" /><input v-model="annotation[path].page_number" type="number" min="1" placeholder="页码" /><input v-model="annotation[path].bounding_box" placeholder="x1,y1,x2,y2" /><input v-model="annotation[path].observed_text" placeholder="图片原文" /></template></div></div>
            <button class="primary-btn" @click="submitAnnotation"><ShieldCheck :size="16" />提交标注</button>
          </details>
          <p class="context-note">第三人裁决需要可核对的分歧内容；当前查询接口未提供，页面不开放裁决提交。</p>
          <div class="lookup"><label>已冻结文档编号<input v-model="jobDocuments" placeholder="多个编号用逗号分隔" /></label><button class="secondary-btn" @click="createJob"><BarChart3 :size="16" />创建配对任务</button></div>
          <p v-if="state.goldMessage" role="status" class="context-note">{{ state.goldMessage }}</p>
          <div v-if="state.job" class="button-row"><StatusBadge :value="state.job.status" /><span>任务编号：{{ state.job.job_id }}</span><button class="icon-btn" title="查询评估任务" @click="queryJob"><RefreshCw :size="16" /></button></div>
        </section>
      </template>
    </ResourceState>
  </div>
</template>

<style scoped>
.effect-stage-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }
.effect-stage { display: grid; gap: 6px; padding: 10px 0; border-top: 1px solid #31454d; font-size: 14px; }
.effect-stage small { overflow-wrap: anywhere; color: #91a8ad; }
.effect-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 14px; margin: 16px 0; }
.effect-metrics > div { display: grid; gap: 5px; }
.effect-metrics span { font-size: 14px; color: #91a8ad; }
.effect-metrics strong { font-size: 20px; }
.effect-form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; margin: 12px 0; }
.effect-form-grid label { display: grid; gap: 5px; font-size: 14px; }
.effect-annotation-list { max-height: 400px; overflow: auto; margin-bottom: 12px; }
.effect-annotation-row { display: grid; grid-template-columns: minmax(120px, 1fr) repeat(5, minmax(90px, 1fr)); gap: 6px; align-items: center; padding: 8px 0; border-top: 1px solid #31454d; }
.effect-annotation-row strong { font-size: 14px; overflow-wrap: anywhere; }
.effect-annotation-row code { display: block; font-weight: 400; }
.effect-annotation-row input, .effect-annotation-row select, .effect-form-grid input { min-width: 0; width: 100%; }
@media (max-width: 850px) { .effect-annotation-row { grid-template-columns: repeat(2, minmax(0, 1fr)); } .effect-annotation-row strong { grid-column: 1 / -1; } }
</style>
