<script setup>
import { ArrowRight, CheckCircle2, CircleAlert, Info, TriangleAlert, X } from 'lucide-vue-next'
import { useNotifications } from '../composables/useNotifications'

const { notifications, dismiss } = useNotifications()
const icons = { success: CheckCircle2, warning: TriangleAlert, error: CircleAlert, info: Info }

function runAction(item) {
  item.onAction?.()
  dismiss(item.id)
}
</script>

<template>
  <aside class="notification-center" aria-label="操作反馈">
    <TransitionGroup name="notification">
      <article
        v-for="item in notifications"
        :key="item.id"
        v-motion
        class="notification-item"
        :class="item.type"
        :initial="{ opacity: 0, x: 18 }"
        :enter="{ opacity: 1, x: 0, transition: { type: 'spring', stiffness: 250, damping: 25 } }"
        :leave="{ opacity: 0, x: 18, transition: { duration: 0.16 } }"
        :role="item.type === 'error' ? 'alert' : 'status'"
        :aria-live="item.type === 'error' ? 'assertive' : 'polite'"
      >
        <component :is="icons[item.type]" class="notification-icon" :size="20" />
        <div>
          <strong>{{ item.title }}</strong>
          <p>{{ item.message }}</p>
          <small v-if="item.traceId">技术关联标识：{{ item.traceId }}</small>
          <button v-if="item.actionLabel" class="notification-action" @click="runAction(item)">
            {{ item.actionLabel }}<ArrowRight :size="15" />
          </button>
        </div>
        <button class="notification-close" :aria-label="`关闭${item.title}提示`" @click="dismiss(item.id)">
          <X :size="16" />
        </button>
      </article>
    </TransitionGroup>
  </aside>
</template>
