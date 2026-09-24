<script setup>
import { computed, nextTick, ref, watch } from 'vue'
import { AlertTriangle, LoaderCircle, X } from 'lucide-vue-next'

const props = defineProps({
  open: Boolean,
  title: { type: String, default: '确认治理操作' },
  confirmLabel: { type: String, default: '确认' },
  danger: Boolean,
  revision: { type: Number, default: null },
  itemCount: { type: Number, default: 1 },
  busy: Boolean,
  returnFocusSelector: { type: String, default: '' },
})
const emit = defineEmits(['close', 'confirm'])
const reason = ref('')
const dialogRef = ref(null)
const reasonRef = ref(null)
let previousFocus = null
const canSubmit = computed(() => reason.value.trim().length > 0 && !props.busy)
watch(() => props.open, async (open) => {
  if (open) {
    previousFocus = document.activeElement
    reason.value = ''
    await nextTick()
    reasonRef.value?.focus()
  } else {
    await nextTick()
    const fallback = props.returnFocusSelector ? document.querySelector(props.returnFocusSelector) : null
    const target = previousFocus && previousFocus !== document.body && previousFocus.isConnected ? previousFocus : fallback
    target?.focus?.()
    previousFocus = null
  }
})

function close() {
  if (!props.busy) emit('close')
}

function handleKeydown(event) {
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
    return
  }
  if (event.key !== 'Tab') return
  const controls = Array.from(dialogRef.value?.querySelectorAll(
    'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled])',
  ) || [])
  if (!controls.length) return
  const first = controls[0]
  const last = controls.at(-1)
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="dialog-motion" mode="out-in">
      <div v-if="open" class="dialog-backdrop" @click.self="close">
        <section ref="dialogRef" class="dialog" role="dialog" aria-modal="true" :aria-label="title" aria-describedby="governance-dialog-description" @keydown="handleKeydown">
          <header><div><AlertTriangle :size="19" /><strong>{{ title }}</strong></div><button class="icon-btn" title="关闭" :disabled="busy" @click="close"><X :size="17" /></button></header>
          <p id="governance-dialog-description">该动作会写入不可变审计记录。请输入明确原因。<template v-if="itemCount > 1">同一原因将应用到 {{ itemCount }} 条记录。</template></p>
          <label>操作原因<textarea ref="reasonRef" v-model="reason" maxlength="2000" rows="4" :disabled="busy" placeholder="说明判断依据和期望结果" /></label>
          <div v-if="revision !== null" class="revision-note">提交修订号 {{ revision }}；数据变化时系统会要求读取最新状态。</div>
          <div v-else-if="itemCount > 1" class="revision-note">每条记录按当前修订号独立提交；失败项不会阻断其他记录。</div>
          <div v-if="busy" class="dialog-busy" role="status" aria-live="polite"><LoaderCircle class="spin" :size="16" />正在提交并等待后端确认，请勿重复操作</div>
          <footer><button class="secondary-btn" :disabled="busy" @click="close">取消</button><button class="primary-btn" :class="{ danger }" :disabled="!canSubmit" @click="emit('confirm', reason.trim())"><LoaderCircle v-if="busy" class="spin" :size="16" />{{ busy ? '正在提交...' : confirmLabel }}</button></footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
