<script setup>
defineProps({ value: { type: String, default: 'unknown' } })

const statusLabels = {
  pending: '待准入', approved: '已批准', quarantined: '已隔离', rejected: '已拒绝',
  suspended: '已暂停', invalidated: '已失效', processing: '处理中', completed: '已完成',
  failed: '失败', indexed: '已索引', open: '未解决', accepted: '已绑定',
  review_required: '需审核', unresolved: '未解析', confirmed_correct: '确认正确',
  corrected: '已纠正', confirmed_incorrect: '确认错误', disabled: '已禁用',
  received: '已接收', pending_review: '待人工审核', idle: '未开始', active: '执行中',
  waiting: '等待后端', done: '已完成', not_reported: '未报告',
  recommend_approval: '建议批准', recommend_quarantine: '建议隔离', recommend_rejection: '建议拒绝',
  passed: '通过', warning: '风险', failed: '失败', unknown: '未知',
  dismissed: '已忽略', resolved: '已解决',
  conflicting: '结果冲突', corroborated: '结果一致', ocr_only: '仅文字识别',
  vision_only: '仅视觉识别', unavailable: '不可用', error: '异常',
  retryable: '可重试', failed_retryable: '等待重试', memory_pending: '记忆待处理',
  running: '运行中', overall: '总体', ready_to_activate: '未激活·待校验',
  index_active: '已激活',
  unavailable: '不可用',
  no_data: '未读取',
  quarantined: '已隔离',
  failed_retryable: '等待重试',
  consistent: '结果一致',
  invalid: '无效',
}
</script>

<template>
  <span class="status-badge" :class="`status-${value}`">{{ statusLabels[value] || value }}</span>
</template>
