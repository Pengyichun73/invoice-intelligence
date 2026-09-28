<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  Archive,
  BarChart3,
  BookOpenCheck,
  ChevronDown,
  Database,
  FileSearch,
  FileText,
  GitCompareArrows,
  Home,
  Menu,
  LogIn,
  LogOut,
  ScrollText,
  ShieldCheck,
  X,
} from 'lucide-vue-next'
import AuditLog from './views/AuditLog.vue'
import ConflictGovernance from './views/ConflictGovernance.vue'
import Dashboard from './views/Dashboard.vue'
import EvaluationResults from './views/EvaluationResults.vue'
import GovernanceOperations from './views/GovernanceOperations.vue'
import ExampleGovernance from './views/ExampleGovernance.vue'
import ExtractionWorkbench from './views/ExtractionWorkbench.vue'
import FieldSemantics from './views/FieldSemantics.vue'
import IndexGovernance from './views/IndexGovernance.vue'
import MemoryAdmissions from './views/MemoryAdmissions.vue'
import NotificationCenter from './components/NotificationCenter.vue'
import RequestProgress from './components/RequestProgress.vue'
import { loginEnabled, restoreSession, signIn, signOut } from './auth/session.js'

const authRequired = loginEnabled()
const identity = ref(null)
const authLoading = ref(authRequired)
const authError = ref('')

async function loadIdentity() {
  try {
    const user = await restoreSession()
    identity.value = user && !user.expired ? user : null
    if (window.location.search.includes('code=')) {
      window.history.replaceState(null, '', window.location.pathname + window.location.hash)
    }
  } catch {
    authError.value = '登录未完成，请重试。'
  } finally {
    authLoading.value = false
  }
}

async function beginSignIn() {
  authError.value = ''
  authLoading.value = true
  try {
    await signIn()
  } catch {
    authLoading.value = false
    authError.value = '认证服务暂时不可用，请稍后重试。'
  }
}

const groups = [
  {
    key: 'workbench',
    label: '工作台',
    items: [
      { key: 'dashboard', label: '总览', icon: Home, component: Dashboard },
      { key: 'extraction', label: '发票提取', icon: FileSearch, component: ExtractionWorkbench },
    ],
  },
  {
    key: 'memory',
    label: '可信记忆',
    items: [
      { key: 'admissions', label: '记忆准入', icon: ShieldCheck, component: MemoryAdmissions },
      { key: 'examples', label: '案例库', icon: Archive, component: ExampleGovernance },
    ],
  },
  {
    key: 'semantics',
    label: '字段语义',
    items: [
      { key: 'semantics', label: '字段目录', icon: BookOpenCheck, component: FieldSemantics },
      { key: 'conflicts', label: '冲突处理', icon: GitCompareArrows, component: ConflictGovernance },
    ],
  },
  {
    key: 'operations',
    label: '运行治理',
    items: [
      { key: 'indexes', label: '索引治理', icon: Database, component: IndexGovernance },
      { key: 'evaluations', label: '评估结果', icon: BarChart3, component: EvaluationResults },
      { key: 'operations', label: '后台操作', icon: ShieldCheck, component: GovernanceOperations },
      { key: 'audits', label: '审计记录', icon: ScrollText, component: AuditLog },
    ],
  },
]

const views = groups.flatMap((group) => group.items.map((item) => ({ ...item, group: group.key })))
const viewKeys = new Set(views.map((item) => item.key))
const hashView = window.location.hash.slice(1)
const storedView = window.localStorage.getItem('invoice-console-view')
const initial = viewKeys.has(hashView) ? hashView : storedView
const activeKey = ref(views.some((item) => item.key === initial) ? initial : 'dashboard')
const mobileOpen = ref(false)
const drawerRef = ref(null)
const menuTriggerRef = ref(null)
const reducedMotion = ref(window.matchMedia('(prefers-reduced-motion: reduce)').matches)
const active = computed(() => views.find((item) => item.key === activeKey.value) || views[0])
const activeGroup = computed(() => groups.find((group) => group.key === active.value.group) || groups[0])
let motionMediaQuery
const handleMotionPreference = (event) => { reducedMotion.value = event.matches }

watch(mobileOpen, (open) => {
  document.body.classList.toggle('drawer-open', open)
  if (open) {
    nextTick(() => drawerRef.value?.focus())
  } else {
    nextTick(() => menuTriggerRef.value?.focus())
  }
})

async function announceView() {
  await nextTick()
  document.title = `${active.value.label} · 发票智能中枢`
  const heading = document.querySelector('.page-container h1')
  if (heading) {
    heading.setAttribute('tabindex', '-1')
    heading.focus({ preventScroll: true })
  }
  window.scrollTo({ top: 0, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
}

function setView(key) {
  const normalized = viewKeys.has(key) ? key : 'dashboard'
  activeKey.value = normalized
  mobileOpen.value = false
  window.localStorage.setItem('invoice-console-view', normalized)
  document.title = `${active.value.label} · 发票智能中枢`
}

function navigate(key) {
  if (!viewKeys.has(key)) key = 'dashboard'
  if (activeKey.value === key && window.location.hash === `#${key}`) {
    mobileOpen.value = false
    return
  }
  window.history.pushState(null, '', `#${key}`)
  setView(key)
}

function navigateGroup(group) {
  navigate(group.items[0].key)
}

function restoreFromHistory() {
  const key = window.location.hash.slice(1)
  if (!viewKeys.has(key)) {
    window.history.replaceState(null, '', '#dashboard')
    setView('dashboard')
    return
  }
  setView(key)
}

onMounted(() => {
  if (authRequired) loadIdentity()
  window.addEventListener('invoice:auth-expired', handleAuthExpired)
  if (!viewKeys.has(hashView)) window.history.replaceState(null, '', `#${activeKey.value}`)
  window.addEventListener('popstate', restoreFromHistory)
  motionMediaQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  motionMediaQuery.addEventListener?.('change', handleMotionPreference)
  announceView()
})
onBeforeUnmount(() => {
  window.removeEventListener('invoice:auth-expired', handleAuthExpired)
  window.removeEventListener('popstate', restoreFromHistory)
  motionMediaQuery?.removeEventListener?.('change', handleMotionPreference)
  document.body.classList.remove('drawer-open')
})

function handleAuthExpired() {
  identity.value = null
  authError.value = '会话已过期，请重新登录。'
}
</script>

<template>
  <main v-if="authRequired && !identity" class="auth-screen">
    <div class="auth-panel">
      <span class="brand-symbol"><FileText :size="24" /></span>
      <h1>发票智能中枢</h1>
      <p v-if="authLoading">正在确认身份…</p>
      <p v-else-if="authError" role="alert">{{ authError }}</p>
      <p v-else>请使用验收环境账号登录</p>
      <button v-if="!authLoading" type="button" class="auth-command" @click="beginSignIn"><LogIn :size="18" />登录</button>
    </div>
  </main>
  <div v-else class="app-shell">
    <RequestProgress />
    <header class="global-header">
      <button class="product-brand" aria-label="返回总览" @click="navigate('dashboard')">
        <span class="brand-symbol"><FileText :size="19" /></span>
        <span><strong>发票智能中枢</strong><small>INVOICE INTELLIGENCE</small></span>
      </button>

      <nav class="primary-navigation" aria-label="产品主导航">
        <button
          v-for="group in groups"
          :key="group.key"
          :class="{ active: activeGroup.key === group.key }"
          @click="navigateGroup(group)"
        >
          {{ group.label }}
        </button>
      </nav>

      <div class="workspace-state">
        <i />
        <span><strong>{{ identity?.profile?.preferred_username || '受控工作区' }}</strong><small>身份由可信上下文确认</small></span>
      </div>
      <button v-if="authRequired" class="icon-btn" title="退出登录" aria-label="退出登录" @click="signOut"><LogOut :size="18" /></button>
      <button ref="menuTriggerRef" class="icon-btn menu-trigger" title="打开全部功能" @click="mobileOpen = true">
        <Menu :size="20" />
      </button>
    </header>

    <nav class="section-navigation" :aria-label="`${activeGroup.label}导航`">
      <div class="section-navigation-inner">
        <span class="section-label">{{ activeGroup.label }}</span>
        <button
          v-for="item in activeGroup.items"
          :key="item.key"
          :class="{ active: activeKey === item.key }"
          @click="navigate(item.key)"
        >
          <component :is="item.icon" :size="15" />{{ item.label }}
        </button>
      </div>
    </nav>

    <main id="main-content" class="main-area" tabindex="-1">
      <div class="page-container">
        <Transition name="page-swap" mode="out-in" @after-enter="announceView">
          <KeepAlive>
            <component :is="active.component" :key="active.key" @navigate="navigate" />
          </KeepAlive>
        </Transition>
      </div>
    </main>

    <NotificationCenter />

    <nav class="mobile-dock" aria-label="移动端核心导航">
      <button
        v-for="group in groups"
        :key="group.key"
        :class="{ active: activeGroup.key === group.key }"
        @click="navigateGroup(group)"
      >
        <component :is="group.items[0].icon" :size="18" />
        <span>{{ group.label }}</span>
      </button>
      <button @click="mobileOpen = true"><Menu :size="18" /><span>全部</span></button>
    </nav>

    <Transition name="drawer-scrim">
      <div v-if="mobileOpen" class="nav-scrim" @click="mobileOpen = false" />
    </Transition>
    <Transition name="drawer-panel">
      <aside ref="drawerRef" v-if="mobileOpen" class="function-drawer" aria-label="全部功能" aria-modal="true" role="dialog" tabindex="-1" @keydown.esc="mobileOpen = false">
      <header>
        <div><small>功能导航</small><strong>全部工作区</strong></div>
        <button class="icon-btn" title="关闭菜单" @click="mobileOpen = false"><X :size="19" /></button>
      </header>
      <section v-for="group in groups" :key="group.key">
        <h2>{{ group.label }}<ChevronDown :size="14" /></h2>
        <button
          v-for="item in group.items"
          :key="item.key"
          :class="{ active: activeKey === item.key }"
          @click="navigate(item.key)"
        >
          <component :is="item.icon" :size="17" />
          <span>{{ item.label }}</span>
        </button>
      </section>
      </aside>
    </Transition>
  </div>
</template>
