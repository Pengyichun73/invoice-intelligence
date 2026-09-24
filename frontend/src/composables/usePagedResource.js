import { reactive } from 'vue'

export function usePagedResource(loader) {
  const state = reactive({ items: [], payload: null, loading: false, error: '', cursor: null, nextCursor: null, history: [] })

  async function load({ reset = false } = {}) {
    if (state.loading) return
    if (reset) {
      state.cursor = null
      state.history = []
    }
    state.loading = true
    state.error = ''
    try {
      const payload = await loader(state.cursor)
      state.payload = payload
      state.items = payload?.items || []
      state.nextCursor = payload?.next_cursor || null
    } catch (error) {
      state.error = error?.message || '读取失败'
      throw error
    } finally {
      state.loading = false
    }
  }

  async function next() {
    if (!state.nextCursor) return
    state.history.push(state.cursor)
    state.cursor = state.nextCursor
    await load()
  }

  async function previous() {
    if (!state.history.length) return
    state.cursor = state.history.pop() ?? null
    await load()
  }

  return { state, load, next, previous }
}
