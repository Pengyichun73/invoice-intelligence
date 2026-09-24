<script setup>
import { computed, reactive, ref } from 'vue'
import { BarChart3, Search } from 'lucide-vue-next'
import PageHeader from '../components/PageHeader.vue'
import ResourceState from '../components/ResourceState.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { displayLabel, formatDate, formatScore } from '../api/client'
import { governanceApi } from '../api/governance'

const runId = ref('')
const state = reactive({ run: null, loading: false, error: '' })
const selectedVariant = ref('')
const selectedBucket = ref('overall')
const result = computed(() => state.run?.results?.find((item) => item.variant === selectedVariant.value) || null)
const bucket = computed(() => selectedBucket.value === 'overall' ? result.value?.overall : result.value?.buckets?.[Number(selectedBucket.value)] || null)
const variantComparison = computed(() => (state.run?.results || []).map((item) => {
  const retrieval = item.overall.retrieval_metrics?.[0] || {}
  const trusted = item.overall.trusted_memory_field_metrics?.[0] || {}
  return {
    variant: item.variant,
    recall: retrieval.recall_at_k,
    emptyRate: retrieval.empty_retrieval_rate,
    fieldAccuracy: item.overall.extraction_metric?.field_accuracy,
    aliasAccuracy: trusted.alias_binding_accuracy,
    harmfulRate: trusted.harmful_memory_admission_rate,
  }
}))
async function load() { if (!runId.value.trim()) { state.error = '请输入评估批次编号'; return } state.loading = true; state.error = ''; try { state.run = await governanceApi.evaluation(runId.value.trim()); selectedVariant.value = state.run.results?.[0]?.variant || ''; selectedBucket.value = 'overall' } catch (error) { state.error = error.message } finally { state.loading = false } }
const metrics = computed(() => {
  const value = bucket.value
  if (!value) return []
  const retrieval = value.retrieval_metrics?.[0] || {}
  const extraction = value.extraction_metric || {}
  const trusted = value.trusted_memory_field_metrics?.[0] || {}
  return [
    ['前 K 项召回率', retrieval.recall_at_k], ['前 K 项命中率', retrieval.hit_rate_at_k], ['平均倒数排名', retrieval.mrr], ['排序质量', retrieval.ndcg_at_k], ['正负例区分度', retrieval.positive_negative_separation], ['空检索比例', retrieval.empty_retrieval_rate],
    ['字段准确率', extraction.field_accuracy], ['缺失识别准确率', extraction.missing_recognition_accuracy], ['候选命中率', extraction.candidate_hit_rate], ['错误自动填充数', extraction.erroneous_auto_filled_value_count], ['需审核判断准确率', extraction.review_required_precision], ['需审核召回率', extraction.review_required_recall],
    ['记忆批准准确率', trusted.memory_approval_precision], ['有害记忆准入率', trusted.harmful_memory_admission_rate], ['隔离比例', trusted.quarantine_rate], ['别名绑定准确率', trusted.alias_binding_accuracy], ['前 K 项字段召回率', trusted.top_k_field_recall], ['错误字段自动填充数', trusted.wrong_field_auto_fill_count], ['记忆帮助率', trusted.memory_helpfulness_rate], ['误导性检索比例', trusted.misleading_retrieval_rate],
  ]
})
</script>

<template>
  <div>
    <PageHeader eyebrow="离线评估" title="评估结果" description="只读取离线评估产物；候选方案不能自动修改生产配置。" />
    <section class="toolbar lookup-wide"><label>评估批次编号<input v-model="runId" placeholder="输入评估批次编号" @keyup.enter="load" /></label><button class="primary-btn" @click="load"><Search :size="16" />查询</button></section>
    <section class="surface">
      <ResourceState :loading="state.loading" :error="state.error" :empty="!state.run" empty-text="输入 evaluation_run_id 查看结果" @retry="load">
        <template v-if="state.run"><header class="section-head"><div><span>{{ displayLabel(state.run.suite, 'evaluation_suite') }}</span><h2>{{ state.run.evaluation_run_id }}</h2></div><StatusBadge :value="state.run.status" /></header><dl class="meta-list columns"><div><dt>评估数据集</dt><dd>{{ state.run.dataset_id }} / {{ state.run.dataset_version }}</dd></div><div><dt>索引版本</dt><dd>{{ state.run.bindings.index_version }}</dd></div><div><dt>模型版本</dt><dd>{{ state.run.bindings.model_version }}</dd></div><div><dt>提示词版本</dt><dd>{{ state.run.bindings.prompt_version }}</dd></div><div><dt>阈值策略版本</dt><dd>{{ state.run.bindings.threshold_version }}</dd></div><div><dt>完成时间</dt><dd>{{ formatDate(state.run.completed_at) }}</dd></div></dl><h3 class="subheading">方案总体对比</h3><div class="table-wrap"><table><thead><tr><th>方案</th><th>前 K 项召回率</th><th>空检索比例</th><th>字段准确率</th><th>别名绑定准确率</th><th>有害准入率</th></tr></thead><tbody><tr v-for="item in variantComparison" :key="item.variant"><td>{{ displayLabel(item.variant, 'evaluation_variant') }}</td><td>{{ formatScore(item.recall) }}</td><td>{{ formatScore(item.emptyRate) }}</td><td>{{ formatScore(item.fieldAccuracy) }}</td><td>{{ formatScore(item.aliasAccuracy) }}</td><td>{{ formatScore(item.harmfulRate) }}</td></tr></tbody></table></div><div class="comparison-controls"><label>评估方案<select v-model="selectedVariant"><option v-for="item in state.run.results" :key="item.variant" :value="item.variant">{{ displayLabel(item.variant, 'evaluation_variant') }}</option></select></label><label>数据分组<select v-model="selectedBucket"><option value="overall">总体</option><option v-for="(item, index) in result?.buckets || []" :key="`${item.bucket.dimension}-${item.bucket.value}`" :value="String(index)">{{ displayLabel(item.bucket.dimension, 'bucket_dimension') }} / {{ item.bucket.value }}</option></select></label></div><h3 class="subheading">指标（未校准分数不表示概率）</h3><div class="metric-table"><div v-for="item in metrics" :key="item[0]"><span>{{ item[0] }}</span><strong>{{ formatScore(item[1]) }}</strong></div></div><h3 class="subheading">晋升候选</h3><div class="record-list"><article v-for="item in state.run.promotion_candidates" :key="item.candidate_id"><header><strong>{{ displayLabel(item.candidate_variant, 'evaluation_variant') }}</strong><StatusBadge :value="item.status" /></header><p>基线方案：{{ displayLabel(item.baseline_variant, 'evaluation_variant') }} · 评估依据 {{ item.rationale_codes.length }} 项</p><footer>必须人工审批：{{ item.requires_human_approval ? '是' : '否' }} · 可直接改生产：{{ item.may_modify_production ? '是' : '否' }}</footer></article><div v-if="!state.run.promotion_candidates.length" class="empty-row"><BarChart3 :size="18" />没有晋升候选</div></div></template>
      </ResourceState>
    </section>
  </div>
</template>
