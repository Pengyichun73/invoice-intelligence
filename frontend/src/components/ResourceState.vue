<script setup>
import { AlertCircle, Inbox, LoaderCircle, RefreshCw } from 'lucide-vue-next'

defineProps({
  loading: Boolean,
  error: { type: String, default: '' },
  unavailable: { type: String, default: '' },
  empty: Boolean,
  emptyText: { type: String, default: '暂无数据' },
})
defineEmits(['retry'])
</script>

<template>
  <div v-if="loading" class="resource-state" role="status" aria-live="polite"><LoaderCircle class="spin" :size="22" /><span>正在读取后端状态...</span></div>
  <div v-else-if="error" class="resource-state error" role="alert" aria-live="assertive"><AlertCircle :size="22" /><span>{{ error }}</span><button class="icon-text-btn" @click="$emit('retry')"><RefreshCw :size="15" />重新读取</button></div>
  <div v-else-if="unavailable" class="resource-state unavailable" role="status" aria-live="polite"><AlertCircle :size="22" /><span>{{ unavailable }}</span><button class="icon-text-btn" @click="$emit('retry')"><RefreshCw :size="15" />重新读取</button></div>
  <div v-else-if="empty" class="resource-state"><Inbox :size="24" /><span>{{ emptyText }}</span></div>
  <slot v-else />
</template>
