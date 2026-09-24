<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import {
  BookOpenCheck,
  CheckCircle2,
  Database,
  Play,
  RefreshCw,
  RotateCcw,
} from 'lucide-vue-next'
import ActionDialog from '../components/ActionDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { displayLabel, formatDate, formatScore } from '../api/client'
import { governanceApi } from '../api/governance'
import { useNotifications } from '../composables/useNotifications'

const notices = useNotifications()

const mode = ref('examples')
const caseVersion = ref('')
const semanticVersion = ref('')
const caseState = reactive({ index: null, loading: false, error: '' })
const semanticState = reactive({ index: null, loading: false, error: '' })
const operation = reactive({ busy: false, message: '' })
const ocr = reactive({ metrics: null, error: '' })
const caseRebuild = reactive({
  configOpen: false,
  confirmOpen: false,
  busy: false,
  index_version: '',
  schema_version: '',
  dense_model_version: '',
  sparse_model_version: '',
  rerank_model_version: '',
  prompt_version: '',
})
const semanticRebuild = reactive({
  open: false,
  busy: false,
  index_version: '',
  schema_version: '',
  catalog_version: '',
  dense_model_version: '',
  sparse_model_version: '',
})

const caseCounts = computed(() => caseState.index?.projection_counts || {})
const semanticCounts = computed(() => {
  const index = semanticState.index
  if (!index) return {}
  return {
    pending: index.pending_count,
    processing: index.processing_count,
    indexed: index.indexed_count,
    failed: index.failed_count,
    invalidated: index.invalidated_count,
    total: index.pending_count + index.processing_count + index.indexed_count + index.failed_count + index.invalidated_count,
  }
})
const projectionStatuses = ['pending', 'processing', 'indexed', 'failed', 'invalidated', 'total']

function resetOperation() {
  operation.message = ''
}

async function loadCaseIndex() {
  if (!caseVersion.value.trim()) {
    caseState.error = '请输入案例索引版本'
    return
  }
  caseState.loading = true
  caseState.error = ''
  resetOperation()
  try {
    caseState.index = await governanceApi.index(caseVersion.value.trim())
  } catch (error) {
    caseState.error = error.message
  } finally {
    caseState.loading = false
  }
}

async function loadSemanticIndex() {
  if (!semanticVersion.value.trim()) {
    semanticState.error = '请输入字段语义索引版本'
    return
  }
  semanticState.loading = true
  semanticState.error = ''
  resetOperation()
  try {
    semanticState.index = await governanceApi.fieldSemanticIndex(semanticVersion.value.trim())
  } catch (error) {
    semanticState.error = error.message
  } finally {
    semanticState.loading = false
  }
}

async function projectCaseIndex() {
  operation.busy = true
  resetOperation()
  caseState.error = ''
  try {
    const result = await governanceApi.projectIndex(caseState.index.index_version)
    caseState.index = result.index
    operation.message = '本批领取 ' + result.claimed + ' 条，成功 ' + result.indexed + ' 条，失败 ' + result.failed + ' 条'
    const notify = result.failed > 0 ? notices.warning : notices.success
    notify('案例索引批次已处理', operation.message)
  } catch (error) {
    caseState.error = error.message
    notices.error('案例索引处理未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  } finally {
    operation.busy = false
  }
}

async function projectSemanticIndex() {
  operation.busy = true
  resetOperation()
  semanticState.error = ''
  try {
    const result = await governanceApi.projectFieldSemanticIndex(semanticState.index.index_version)
    semanticState.index = result.index
    operation.message = '本批领取 ' + result.claimed + ' 条，成功 ' + result.indexed + ' 条，失败 ' + result.failed + ' 条'
    const notify = result.failed > 0 ? notices.warning : notices.success
    notify('字段索引批次已处理', operation.message)
  } catch (error) {
    semanticState.error = error.message
    notices.error('字段索引处理未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  } finally {
    operation.busy = false
  }
}

async function activateCaseIndex() {
  operation.busy = true
  resetOperation()
  caseState.error = ''
  try {
    caseState.index = await governanceApi.activateIndex(caseState.index.index_version)
    operation.message = '案例索引版本已激活'
    notices.success('案例索引已切换', '后端已记录当前激活版本；这不等于实时检查 Milvus 健康状态。')
  } catch (error) {
    caseState.error = error.message
    notices.error('案例索引切换未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  } finally {
    operation.busy = false
  }
}

async function activateSemanticIndex() {
  operation.busy = true
  resetOperation()
  semanticState.error = ''
  try {
    semanticState.index = await governanceApi.activateFieldSemanticIndex(semanticState.index.index_version)
    operation.message = '字段语义索引版本已激活'
    notices.success('字段语义索引已切换', '后端已记录当前激活版本；这不等于实时检查 Milvus 健康状态。')
  } catch (error) {
    semanticState.error = error.message
    notices.error('字段索引切换未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  } finally {
    operation.busy = false
  }
}

function openCaseRebuild() {
  caseRebuild.index_version ||= caseVersion.value.trim()
  caseRebuild.configOpen = true
}

function confirmCaseRebuild() {
  const required = ['index_version', 'schema_version', 'dense_model_version', 'prompt_version']
  if (required.some((key) => !caseRebuild[key].trim())) {
    caseState.error = '请填写索引、数据结构、向量模型和提示词版本'
    return
  }
  caseRebuild.configOpen = false
  caseRebuild.confirmOpen = true
}

async function submitCaseRebuild(reason) {
  caseRebuild.busy = true
  caseState.error = ''
  try {
    caseState.index = await governanceApi.rebuildIndex({
      reason,
      index_version: caseRebuild.index_version.trim(),
      schema_version: caseRebuild.schema_version.trim(),
      dense_model_version: caseRebuild.dense_model_version.trim(),
      sparse_model_version: caseRebuild.sparse_model_version.trim() || null,
      rerank_model_version: caseRebuild.rerank_model_version.trim() || null,
      prompt_version: caseRebuild.prompt_version.trim(),
    })
    caseVersion.value = caseRebuild.index_version.trim()
    caseRebuild.confirmOpen = false
    operation.message = '案例索引重建任务已登记，等待投影处理'
    notices.success('案例索引重建已登记', 'PostgreSQL 已创建投影任务，Milvus 尚未被本次响应确认完成。')
  } catch (error) {
    caseState.error = error.message
    caseRebuild.confirmOpen = false
    notices.error('案例索引登记未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  } finally {
    caseRebuild.busy = false
  }
}

function openSemanticRebuild() {
  semanticRebuild.index_version ||= semanticVersion.value.trim()
  semanticRebuild.open = true
}

async function submitSemanticRebuild() {
  const required = ['index_version', 'schema_version', 'catalog_version', 'dense_model_version']
  if (required.some((key) => !semanticRebuild[key].trim())) {
    semanticState.error = '请填写索引、数据结构、字段目录和向量模型版本'
    return
  }
  semanticRebuild.busy = true
  semanticState.error = ''
  try {
    semanticState.index = await governanceApi.rebuildFieldSemanticIndex({
      index_version: semanticRebuild.index_version.trim(),
      schema_version: semanticRebuild.schema_version.trim(),
      catalog_version: semanticRebuild.catalog_version.trim(),
      dense_model_version: semanticRebuild.dense_model_version.trim(),
      sparse_model_version: semanticRebuild.sparse_model_version.trim() || null,
    })
    semanticVersion.value = semanticRebuild.index_version.trim()
    semanticRebuild.open = false
    operation.message = '字段语义索引重建任务已登记，等待投影处理'
    notices.success('字段索引重建已登记', 'PostgreSQL 已创建投影任务，Milvus 尚未被本次响应确认完成。')
  } catch (error) {
    semanticState.error = error.message
    notices.error('字段索引登记未完成', error.recovery ? `${error.message}。${error.recovery}` : '操作未完成，请稍后重试。')
  } finally {
    semanticRebuild.busy = false
  }
}

async function loadOcrMetrics() {
  try {
    ocr.metrics = await governanceApi.ocrMetrics()
  } catch (error) {
    ocr.error = error.message
  }
}

onMounted(loadOcrMetrics)
</script>

<template>
  <div>
    <PageHeader eyebrow="运行治理" title="索引治理" description="PostgreSQL 保存权威投影状态；Milvus 仅承载可重建的脱敏派生索引。">
      <button v-if="mode === 'examples'" id="case-rebuild-trigger" class="primary-btn" @click="openCaseRebuild"><RotateCcw :size="16" />登记案例索引</button>
      <button v-else class="primary-btn" @click="openSemanticRebuild"><RotateCcw :size="16" />登记字段索引</button>
    </PageHeader>

    <div class="segmented-control index-mode" aria-label="索引类型">
      <button :class="{ active: mode === 'examples' }" @click="mode = 'examples'"><Database :size="16" />案例记忆索引</button>
      <button :class="{ active: mode === 'semantics' }" @click="mode = 'semantics'"><BookOpenCheck :size="16" />字段语义索引</button>
    </div>

    <div class="alert warning"><Database :size="18" /><span>“登记重建”只创建 PostgreSQL 投影任务；“已索引”是登记状态。激活时后端才执行完整性校验，页面查询和 /ready 均不是生产验收证明。</span></div>

    <template v-if="mode === 'examples'">
      <section class="toolbar lookup-wide">
        <label>案例索引版本<input v-model="caseVersion" placeholder="输入案例索引版本" @keyup.enter="loadCaseIndex" /></label>
        <button class="secondary-btn" :disabled="caseState.loading" @click="loadCaseIndex"><RefreshCw :size="16" />查询状态</button>
      </section>
      <section class="surface">
        <ResourceState :loading="caseState.loading" :error="caseState.error" :empty="!caseState.index" empty-text="输入版本查询案例索引状态" @retry="loadCaseIndex">
          <template v-if="caseState.index">
            <header class="section-head"><div><span>案例记忆索引</span><h2>{{ caseState.index.index_version }}</h2></div><StatusBadge :value="caseState.index.is_valid ? (caseState.index.is_active ? 'index_active' : 'ready_to_activate') : 'invalidated'" /></header>
            <div class="button-row">
              <button class="secondary-btn" :disabled="operation.busy || !(caseCounts.pending || caseCounts.failed)" @click="projectCaseIndex"><Play :size="15" />处理待投影</button>
              <button class="primary-btn" :disabled="operation.busy || caseState.index.is_active || caseCounts.pending || caseCounts.processing || caseCounts.failed" @click="activateCaseIndex"><CheckCircle2 :size="15" />激活版本</button>
              <span v-if="operation.message" class="context-note">{{ operation.message }}</span>
            </div>
            <div class="count-grid"><div v-for="key in projectionStatuses" :key="key"><span>{{ key === 'total' ? '总数' : displayLabel(key, 'projection_status') }}</span><strong>{{ caseCounts[key] ?? 0 }}</strong></div></div>
            <details class="technical-details"><summary>查看版本与模型绑定</summary><dl class="meta-list columns"><div><dt>数据结构版本</dt><dd>{{ caseState.index.schema_version }}</dd></div><div><dt>向量模型版本</dt><dd>{{ caseState.index.dense_model_version }}</dd></div><div><dt>关键词模型版本</dt><dd>{{ caseState.index.sparse_model_version || '未配置' }}</dd></div><div><dt>重排模型版本</dt><dd>{{ caseState.index.rerank_model_version || '未配置' }}</dd></div><div><dt>提示词版本</dt><dd>{{ caseState.index.prompt_version }}</dd></div><div><dt>创建时间</dt><dd>{{ formatDate(caseState.index.created_at) }}</dd></div></dl></details>
            <h3 class="subheading">检索运行指标（分数不是概率）</h3>
            <div class="metric-table"><div><span>空检索比例</span><strong>{{ formatScore(caseState.index.metrics.empty_retrieval_rate) }}</strong></div><div><span>正例命中率</span><strong>{{ formatScore(caseState.index.metrics.positive_hit_rate) }}</strong></div><div><span>负例命中率</span><strong>{{ formatScore(caseState.index.metrics.negative_hit_rate) }}</strong></div><div><span>需人工审核比例</span><strong>{{ formatScore(caseState.index.metrics.review_required_rate) }}</strong></div><div><span>远程模型错误率</span><strong>{{ formatScore(caseState.index.metrics.remote_model_error_rate) }}</strong></div><div><span>模型处理量</span><strong>{{ caseState.index.metrics.total_input_tokens + caseState.index.metrics.total_output_tokens }}</strong></div></div>
          </template>
        </ResourceState>
      </section>
    </template>

    <template v-else>
      <section class="toolbar lookup-wide">
        <label>字段语义索引版本<input v-model="semanticVersion" placeholder="输入字段语义索引版本" @keyup.enter="loadSemanticIndex" /></label>
        <button class="secondary-btn" :disabled="semanticState.loading" @click="loadSemanticIndex"><RefreshCw :size="16" />查询状态</button>
      </section>
      <section class="surface">
        <ResourceState :loading="semanticState.loading" :error="semanticState.error" :empty="!semanticState.index" empty-text="输入版本查询字段语义索引状态" @retry="loadSemanticIndex">
          <template v-if="semanticState.index">
            <header class="section-head"><div><span>字段语义索引</span><h2>{{ semanticState.index.index_version }}</h2></div><StatusBadge :value="semanticState.index.is_valid ? (semanticState.index.is_active ? 'index_active' : 'ready_to_activate') : 'invalidated'" /></header>
            <div class="button-row">
              <button class="secondary-btn" :disabled="operation.busy || !(semanticCounts.pending || semanticCounts.failed)" @click="projectSemanticIndex"><Play :size="15" />处理待投影</button>
              <button class="primary-btn" :disabled="operation.busy || semanticState.index.is_active || semanticCounts.pending || semanticCounts.processing || semanticCounts.failed" @click="activateSemanticIndex"><CheckCircle2 :size="15" />激活版本</button>
              <span v-if="operation.message" class="context-note">{{ operation.message }}</span>
            </div>
            <div class="count-grid"><div v-for="key in projectionStatuses" :key="key"><span>{{ key === 'total' ? '总数' : displayLabel(key, 'projection_status') }}</span><strong>{{ semanticCounts[key] ?? 0 }}</strong></div></div>
            <details class="technical-details"><summary>查看目录与模型绑定</summary><dl class="meta-list columns"><div><dt>数据结构版本</dt><dd>{{ semanticState.index.schema_version }}</dd></div><div><dt>字段目录版本</dt><dd>{{ semanticState.index.catalog_version }}</dd></div><div><dt>向量模型版本</dt><dd>{{ semanticState.index.dense_model_version }}</dd></div><div><dt>关键词模型版本</dt><dd>{{ semanticState.index.sparse_model_version || '未配置' }}</dd></div><div><dt>创建时间</dt><dd>{{ formatDate(semanticState.index.created_at) }}</dd></div><div><dt>激活时间</dt><dd>{{ formatDate(semanticState.index.activated_at) }}</dd></div></dl></details>
          </template>
        </ResourceState>
      </section>
    </template>

    <section v-if="ocr.metrics" class="surface">
      <header class="section-head"><div><span>运行概览</span><h2>多源文字识别</h2></div><StatusBadge :value="ocr.metrics.unavailable_count ? 'warning' : 'completed'" /></header>
      <div class="metric-table"><div><span>调用次数 / 页面数</span><strong>{{ ocr.metrics.provider_call_count }} / {{ ocr.metrics.page_call_count }}</strong></div><div><span>平均调用耗时</span><strong>{{ Math.round(ocr.metrics.average_call_latency_ms) }} 毫秒</strong></div><div><span>结果一致 / 结果冲突</span><strong>{{ ocr.metrics.corroborated_count }} / {{ ocr.metrics.conflicting_count }}</strong></div><div><span>仅文字识别 / 仅视觉识别</span><strong>{{ ocr.metrics.ocr_only_count }} / {{ ocr.metrics.vision_only_count }}</strong></div></div>
      <details class="technical-details"><summary>查看识别服务与版本</summary><div class="record-list"><article v-for="provider in ocr.metrics.providers" :key="provider.provider_name + ':' + provider.config_version"><header><strong>{{ displayLabel(provider.provider_name, 'provider') }}</strong><span>调用 {{ provider.call_count }} 次</span></header><p>{{ provider.provider_version }} · {{ provider.model_version }} · {{ provider.config_version }}</p></article></div></details>
    </section>

    <div v-if="caseRebuild.configOpen" class="floating-form">
      <header><div><small>案例记忆索引</small><strong>登记重建版本</strong></div><button class="icon-btn" title="关闭" @click="caseRebuild.configOpen = false">×</button></header>
      <div class="form-grid"><label>索引版本<input v-model="caseRebuild.index_version" /></label><label>数据结构版本<input v-model="caseRebuild.schema_version" /></label><label>向量模型版本<input v-model="caseRebuild.dense_model_version" /></label><label>关键词模型版本（可选）<input v-model="caseRebuild.sparse_model_version" /></label><label>重排模型版本（可选）<input v-model="caseRebuild.rerank_model_version" /></label><label>提示词版本<input v-model="caseRebuild.prompt_version" /></label></div>
      <button class="primary-btn" @click="confirmCaseRebuild">下一步填写登记原因</button>
    </div>

    <div v-if="semanticRebuild.open" class="floating-form">
      <header><div><small>字段语义索引</small><strong>登记重建版本</strong></div><button class="icon-btn" title="关闭" @click="semanticRebuild.open = false">×</button></header>
      <div class="form-grid"><label>索引版本<input v-model="semanticRebuild.index_version" /></label><label>数据结构版本<input v-model="semanticRebuild.schema_version" /></label><label>字段目录版本<input v-model="semanticRebuild.catalog_version" /></label><label>向量模型版本<input v-model="semanticRebuild.dense_model_version" /></label><label>关键词模型版本（可选）<input v-model="semanticRebuild.sparse_model_version" /></label></div>
      <button class="primary-btn" :disabled="semanticRebuild.busy" @click="submitSemanticRebuild">{{ semanticRebuild.busy ? '正在登记...' : '登记字段语义索引' }}</button>
    </div>

    <ActionDialog :open="caseRebuild.confirmOpen" :busy="caseRebuild.busy" danger return-focus-selector="#case-rebuild-trigger" title="确认登记案例索引重建" confirm-label="登记重建" @close="caseRebuild.confirmOpen = false" @confirm="submitCaseRebuild" />
  </div>
</template>
