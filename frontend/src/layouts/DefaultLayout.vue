<script setup lang="ts">
/**
 * 默认布局：左侧导航 + 顶栏（市场状态灯 + 时钟 + 数据口径）。
 *
 * 视觉约定：侧栏/顶栏都用半透明深色 + 1px 边框，不用发光阴影；导航激活态由
 * 「左侧 2px 色条 + 略亮的底色 + 主色文字」三重提示，hover 只提亮文字。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity,
  Bell,
  Filter,
  History,
  LayoutDashboard,
  Lightbulb,
  Menu,
  Server,
  Settings,
  ShieldAlert,
  Wallet,
} from 'lucide-vue-next'

import { useMarketStore } from '../stores/market'
import {
  getDataStatus,
  getScopeStats,
  onDataDegraded,
  type DataStatus,
  type ScopeStats,
} from '../api'

const route = useRoute()
const router = useRouter()
const market = useMarketStore()
const scopeStats = ref<ScopeStats | null>(null)
// 手机（经 cpolar 公网访问）没有 224px 的横向空间：侧栏默认收起，点汉堡展开
const sidebarOpen = ref(false)
watch(() => route.fullPath, () => { sidebarOpen.value = false })

// 按「自动交易五段式」组织导航（docs/sas_formulas.md §8）：
// 一选池 → 二选自选 → 三选队列 → 交易池/持仓（同页两 tab），概览/回测为辅助。
const navGroups = [
  {
    title: '概览',
    items: [{ path: '/dashboard', title: '大盘概览', icon: LayoutDashboard }],
  },
  {
    title: '交易流水线',
    items: [
      { path: '/screening', title: '① 一选 · 股票池', icon: Filter },
      { path: '/signals', title: '② 二选 · 自选股', icon: Bell },
      { path: '/advisory', title: '③ 三选 · 交易队列', icon: Lightbulb },
      // ④ 交易池与⑤ 持仓本是同一页的两个 tab，合并为一个菜单项
      { path: '/paper', title: '④ 交易池 · 持仓', icon: Wallet },
    ],
  },
  {
    title: '复盘与配置',
    items: [
      { path: '/backtest', title: '回测中心', icon: History },
      { path: '/ops', title: '运维审计', icon: Server },
      { path: '/settings', title: '系统设置', icon: Settings },
    ],
  },
]

// 顶栏时钟
const now = ref('')
// 市场阶段：未开盘 / 集合竞价 / 交易中 / 午间休市 / 已收盘 / 休市
const phase = ref('')
let timer: number | undefined
let dataTimer: number | undefined
let offDegraded: (() => void) | undefined

// 全局数据新鲜度：SWR 会静默返回旧快照，接口 200 也可能带 degraded=true。
// 状态条独立于任何页面，30s 轮询一次（refresh=true 绕过 GET 缓存）。
const dataStatus = ref<DataStatus | null>(null)
const degradeNotice = ref('')
let degradeAt = 0

function refreshDataStatus() {
  getDataStatus()
    .then((s) => {
      dataStatus.value = s
      // 降级提示保留至少 60s，避免一次抖动就闪一下消失
      if (s.level === 'ok' && Date.now() - degradeAt > 60_000) degradeNotice.value = ''
    })
    .catch(() => {
      dataStatus.value = null // 状态接口自己挂了也不能假装数据正常
    })
}

const dataLevel = computed(() => {
  if (degradeNotice.value && dataStatus.value?.level !== 'error') return 'warn'
  return dataStatus.value?.level ?? 'unknown'
})
const dataChipClass = computed(() => ({
  ok: 'chip-up', warn: 'chip-warn', error: 'chip-down', unknown: 'chip-muted',
}[dataLevel.value]))
const dataDotClass = computed(() => ({
  ok: 'bg-up', warn: 'bg-warn', error: 'bg-down', unknown: 'bg-content-faint',
}[dataLevel.value]))
const dataChipText = computed(() => {
  const s = dataStatus.value
  if (!s) return '数据状态未知'
  if (dataLevel.value === 'error') return '行情数据不可用'
  if (dataLevel.value === 'warn') {
    const age = s.snapshot.age_seconds
    return age == null ? '行情数据陈旧' : `数据陈旧 ${Math.round(age)}s`
  }
  return `数据 ${(s.snapshot.updated_at ?? s.server_time).slice(11, 19)}`
})
const dataTitle = computed(() => {
  const s = dataStatus.value
  if (!s) return '数据状态接口不可用，无法判断页面数据是否新鲜'
  const fails = Object.keys(s.recent_failures).join('、') || '无'
  const minute = s.minute_export.available
    ? `${s.minute_export.latest_date ?? '—'}（导出于 ${s.minute_export.exported_at ?? '—'}）`
    : '不可用'
  return [
    `状态：${s.message}`,
    `快照 ${s.snapshot.rows} 只 · 更新于 ${s.snapshot.updated_at ?? '—'} · 已过 ${s.snapshot.age_seconds ?? '—'}s（TTL ${s.ttl_seconds}s）`,
    `指数 ${s.indices.rows} 条 · 更新于 ${s.indices.updated_at ?? '—'}`,
    `后台刷新中：${s.refreshing.join('、') || '无'} · 近期失败：${fails}`,
    `分钟库最新：${minute} · 交易时段：${s.trading ? '是' : '否'}`,
    degradeNotice.value ? `降级上报：${degradeNotice.value}` : '',
  ].filter(Boolean).join('\n')
})
const bannerText = computed(() => {
  if (dataLevel.value === 'ok' || dataLevel.value === 'unknown') return ''
  return degradeNotice.value || dataStatus.value?.message || '行情数据异常，页面可能显示旧数据'
})

function computePhase(d: Date): string {
  const wd = d.getDay()
  if (wd === 0 || wd === 6) return '休市'
  const m = d.getHours() * 60 + d.getMinutes()
  if (m < 9 * 60 + 15) return '未开盘'
  if (m < 9 * 60 + 30) return '集合竞价'
  if (m <= 11 * 60 + 30) return '交易中'
  if (m < 13 * 60) return '午间休市'
  if (m <= 15 * 60) return '交易中'
  return '已收盘'
}

function tick() {
  const d = new Date()
  now.value = d.toLocaleTimeString('zh-CN', { hour12: false })
  phase.value = computePhase(d)
}

onMounted(() => {
  tick()
  timer = window.setInterval(tick, 1000)
  market.fetchAnalysisStatus()
  getScopeStats().then((d) => (scopeStats.value = d)).catch(() => {})
  refreshDataStatus()
  dataTimer = window.setInterval(refreshDataStatus, 30_000)
  offDegraded = onDataDegraded((e) => {
    degradeAt = Date.now()
    degradeNotice.value = e.message || `接口 ${e.path} 返回降级数据`
  })
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
  if (dataTimer) window.clearInterval(dataTimer)
  offDegraded?.()
})

const statusText = computed(() => phase.value || (market.trading ? '交易中' : '已收盘'))

// 个股详情是流水线里的下钻页，高亮到 ② 二选（自选股入口）
const activePath = computed(() =>
  route.path.startsWith('/stock/') ? '/signals' : route.path
)
</script>

<template>
  <div class="flex h-full">
    <!-- 左侧导航（固定） -->
    <div
      v-if="sidebarOpen"
      class="fixed inset-0 z-40 bg-black/55 lg:hidden"
      aria-hidden="true"
      @click="sidebarOpen = false"
    ></div>
    <aside
      class="fixed left-0 top-0 z-50 flex h-full w-56 flex-col border-r border-base-600/60 bg-base-800/90 backdrop-blur-xl transition-transform duration-200 lg:translate-x-0 lg:bg-base-800/70"
      :class="sidebarOpen ? 'translate-x-0' : '-translate-x-full'"
    >
      <!-- 品牌 -->
      <div class="flex items-center gap-2.5 border-b border-base-600/60 px-5 py-4">
        <div
          class="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-primary-dark ring-1 ring-inset ring-white/15"
        >
          <Activity class="h-4 w-4 text-white" />
        </div>
        <div class="min-w-0">
          <div class="text-[15.5px] font-semibold leading-tight tracking-wide text-content">SAS</div>
          <div class="truncate text-[12.5px] leading-tight text-content-faint">A股智能分析系统</div>
        </div>
      </div>

      <nav class="flex-1 space-y-5 overflow-y-auto px-3 py-4">
        <div v-for="group in navGroups" :key="group.title" class="space-y-0.5">
          <div class="eyebrow px-2.5 pb-1.5">{{ group.title }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="group relative flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-[14px] font-medium transition-colors duration-150"
            @click="sidebarOpen = false"
            :class="
              activePath === item.path
                ? 'bg-primary/[0.12] text-content'
                : 'text-content-muted hover:bg-base-700/60 hover:text-content'
            "
          >
            <span
              class="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full transition-colors"
              :class="activePath === item.path ? 'bg-primary' : 'bg-transparent'"
            ></span>
            <component
              :is="item.icon"
              class="h-4 w-4 shrink-0 transition-colors"
              :class="activePath === item.path ? 'text-primary-light' : 'text-content-faint group-hover:text-content-muted'"
            />
            <span class="truncate">{{ item.title }}</span>
          </router-link>
        </div>
      </nav>

      <!-- 免责声明（侧栏底部常驻） -->
      <div class="border-t border-base-600/60 p-3">
        <div
          class="flex items-start gap-2 rounded-lg border border-warn/25 bg-warn/[0.07] px-2.5 py-2 text-[12px] leading-relaxed text-warn/90"
        >
          <ShieldAlert class="mt-px h-3.5 w-3.5 shrink-0" />
          <span>{{ market.disclaimer }}</span>
        </div>
      </div>
    </aside>

    <!-- 主内容区（为固定侧栏留出宽度） -->
    <div class="flex h-full flex-1 flex-col lg:ml-56">
      <!-- 顶栏（固定） -->
      <header
        class="fixed left-0 right-0 top-0 z-30 flex h-14 items-center justify-between border-b border-base-600/60 bg-base-900/85 px-3 backdrop-blur-xl sm:px-5 lg:left-56"
      >
        <div class="flex min-w-0 items-center gap-2 sm:gap-3">
          <button
            class="btn btn-ghost btn-sm shrink-0 px-2 lg:hidden"
            aria-label="切换导航"
            :aria-expanded="sidebarOpen"
            @click="sidebarOpen = !sidebarOpen"
          >
            <Menu class="h-4 w-4" />
          </button>
          <h1 class="truncate text-subheading tracking-tight">{{ route.meta.title ?? '大盘概览' }}</h1>
          <span
            class="chip"
            :class="market.trading ? 'chip-up' : 'chip-muted'"
          >
            <span
              class="h-1.5 w-1.5 rounded-full"
              :class="market.trading ? 'animate-pulse-slow bg-up' : 'bg-content-faint'"
            />
            {{ statusText }}
          </span>
        </div>
        <div class="flex items-center gap-2">
          <span class="font-num text-[14px] text-content-muted">{{ now }}</span>
          <span class="mx-1 hidden h-4 w-px bg-base-600 sm:block"></span>
          <span
            v-if="scopeStats"
            class="chip chip-muted hidden sm:inline-flex"
            :title="`可交易范围 ${scopeStats.in_scope} 只（主板 ${scopeStats.mainboard} / 创业板 ${scopeStats.cyb}）`"
          >
            范围 {{ scopeStats.in_scope }} · 创业板 {{ scopeStats.cyb }}
          </span>
          <span
            class="chip hidden sm:inline-flex"
            :class="dataChipClass"
            :title="dataTitle"
          >
            <span class="h-1.5 w-1.5 rounded-full" :class="dataDotClass" />
            {{ dataChipText }}
          </span>
          <span class="chip chip-muted hidden md:inline-flex">
            LLM: {{ market.llmAvailable ? market.llmModel : '规则降级' }}
          </span>
        </div>
      </header>

      <!-- 页面内容（顶栏高度补偿） -->
      <main class="flex-1 overflow-y-auto pt-14">
        <div class="mx-auto max-w-[1560px] animate-fade-in px-5 py-5 xl:px-6">
          <!-- 数据降级/陈旧：全局横幅，别让「200 但给的是旧数据」静默过去 -->
          <div
            v-if="bannerText"
            class="mb-4 flex items-start gap-2 rounded-lg border px-3 py-2 text-[12.5px] leading-relaxed"
            :class="dataLevel === 'error'
              ? 'border-down/30 bg-down/[0.08] text-down'
              : 'border-warn/30 bg-warn/[0.08] text-warn'"
          >
            <ShieldAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <div class="min-w-0 flex-1">
              <span class="font-medium">{{ bannerText }}</span>
              <span class="ml-1.5 opacity-80">页面可能显示旧数据，勿当作实时行情；以顶栏状态条的时间为准。</span>
            </div>
            <button
              class="shrink-0 rounded border border-current/30 px-2 py-0.5 text-[12px] opacity-80 hover:opacity-100"
              @click="refreshDataStatus"
            >
              重新检查
            </button>
          </div>
          <router-view v-slot="{ Component, route }">
            <transition name="fade" mode="out-in">
              <keep-alive :max="10">
                <component :is="Component" :key="route.fullPath" />
              </keep-alive>
            </transition>
          </router-view>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
