<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'

const activeRequests = ref(0)

function update(event) {
  activeRequests.value = Math.max(0, Number(event.detail?.active || 0))
}

onMounted(() => window.addEventListener('invoice:request-activity', update))
onBeforeUnmount(() => window.removeEventListener('invoice:request-activity', update))
</script>

<template>
  <Transition name="request-progress">
    <div v-if="activeRequests > 0" class="request-progress" role="status" aria-live="polite">
      <span />
      <em class="sr-only">正在与服务端同步最新状态</em>
    </div>
  </Transition>
</template>

