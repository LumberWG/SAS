<script setup lang="ts">
/**
 * 默认布局：左侧导航 + 顶栏（市场状态灯 + 时钟 + 数据口径）。
 *
 * 视觉约定：侧栏/顶栏都用半透明深色 + 1px 边框，不用发光阴影；导航激活态由
 * 「左侧 2px 色条 + 略亮的底色 + 主色文字」三重提示，hover 只提亮文字。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Activity,
  Bell,
  Filter,
  History,
  LayoutDashboard,
  Lightbulb,
  Settings,
  ShieldAlert,
  Wallet,
} from 'lucide-vue-next'

import { useMarketStore } from '../stores/market'
import { getScopeStats, type ScopeStats } from '../api'

const route = useRoute()
const router = useRouter()
const market = useMarketStore()
const scopeStats = ref<ScopeStats | null>(null)

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
      { path: '/settings', title: '系统设置', icon: Settings },
    ],
  },
]

// 顶栏时钟
const now = ref('')
// 市场阶段：未开盘 / 集合竞价 / 交易中 / 午间休市 / 已收盘 / 休市
const phase = ref('')
let timer: number | undefined

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
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
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
    <aside
      class="fixed left-0 top-0 z-40 flex h-full w-56 flex-col border-r border-base-600/60 bg-base-800/70 backdrop-blur-xl"
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
    <div class="ml-56 flex h-full flex-1 flex-col">
      <!-- 顶栏（固定） -->
      <header
        class="fixed left-56 right-0 top-0 z-30 flex h-14 items-center justify-between border-b border-base-600/60 bg-base-900/85 px-5 backdrop-blur-xl"
      >
        <div class="flex min-w-0 items-center gap-3">
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
          <span class="chip chip-muted">
            LLM: {{ market.llmAvailable ? market.llmModel : '规则降级' }}
          </span>
        </div>
      </header>

      <!-- 页面内容（顶栏高度补偿） -->
      <main class="flex-1 overflow-y-auto pt-14">
        <div class="mx-auto max-w-[1560px] animate-fade-in px-5 py-5 xl:px-6">
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
