import { readonly, reactive } from 'vue'

const state = reactive({ items: [] })
const timers = new Map()
let sequence = 0

function dismiss(id) {
  const timer = timers.get(id)
  if (timer) clearTimeout(timer)
  timers.delete(id)
  const index = state.items.findIndex((item) => item.id === id)
  if (index >= 0) state.items.splice(index, 1)
}

function add(type, title, message, options = {}) {
  const id = `notice-${Date.now()}-${sequence += 1}`
  const duration = options.duration ?? (type === 'error' ? 9000 : 6000)
  state.items.push({
    id,
    type,
    title,
    message,
    actionLabel: options.actionLabel || '',
    onAction: options.onAction || null,
    traceId: options.traceId || null,
  })
  if (duration > 0) timers.set(id, setTimeout(() => dismiss(id), duration))
  return id
}

function withType(type) {
  return (title, message, options) => add(type, title, message, options)
}

export function useNotifications() {
  return {
    notifications: readonly(state.items),
    dismiss,
    success: withType('success'),
    warning: withType('warning'),
    error: withType('error'),
    info: withType('info'),
  }
}
