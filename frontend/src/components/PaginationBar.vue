<script setup>
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'
defineProps({ canPrevious: Boolean, canNext: Boolean, count: { type: Number, default: 0 } })
defineEmits(['previous', 'next'])
</script>

<template>
  <nav class="pagination-bar" aria-label="分页">
    <span>本页 {{ count }} 条</span>
    <div>
      <button class="icon-btn" title="上一页" :disabled="!canPrevious" @click="$emit('previous')"><ChevronLeft :size="17" /></button>
      <button class="icon-btn" title="下一页" :disabled="!canNext" @click="$emit('next')"><ChevronRight :size="17" /></button>
    </div>
  </nav>
</template>
