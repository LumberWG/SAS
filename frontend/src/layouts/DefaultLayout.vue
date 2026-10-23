<script setup lang="ts">
/**
 * 默认布局：左侧导航 + 顶栏（市场状态灯 + 免责声明徽标）。
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
  LineChart,
  ShieldAlert,
  Wallet,
} from 'lucide-vue-next'

import { useMarketStore } from '../stores/market'

const route = useRoute()
const router = useRouter()
const market = useMarketStore()

const navItems = [
  { path: '/dashboard', title: '大盘概览', icon: LayoutDashboard },
  { path: '/screening', title: '选股中心', icon: Filter },
  { path: '/advisory', title: '智能建议', icon: Lightbulb },
  { path: '/paper', title: '模拟交易', icon: Wallet },
  { path: '/stock/600519', title: '个股详情', icon: LineChart },
  { path: '/backtest', title: '回测中心', icon: History },
  { path: '/signals', title: '信号与自选', icon: Bell },
]

// 顶栏时钟
const now = ref('')
let timer: number | undefined

function tick() {
  const d = new Date()
  now.value = d.toLocaleTimeString('zh-CN', { hour12: false })
}

onMounted(() => {
  tick()
  timer = window.setInterval(tick, 1000)
  market.fetchAnalysisStatus()
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

const statusText = computed(() => (market.trading ? '交易中' : '已收盘'))
</script>

<template>
  <div class="flex h-full">
    <!-- 左侧导航（固定） -->
    <aside
      class="fixed left-0 top-0 z-40 flex h-full w-56 flex-col border-r border-base-600/60 bg-base-800/80 backdrop-blur-xl"
    >
      <div class="flex items-center gap-2.5 border-b border-base-600/60 px-5 py-5">
        <div
          class="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-primary-light shadow-glow"
        >
          <Activity class="h-5 w-5 text-white" />
        </div>
        <div>
          <div class="text-[15px] font-semibold leading-tight text-content">SAS</div>
          <div class="text-[11px] leading-tight text-content-faint">A股智能分析系统</div>
        </div>
      </div>

      <nav class="flex-1 space-y-1 px-3 py-4">
        <router-link
          v-for="item in navItems"
          :key="item.path"
          :to="item.path"
          class="group flex items-center gap-3 rounded-lg px-3 py-2.5 text-[13px] font-medium transition-all duration-200"
          :class="
            route.path === item.path ||
            (item.path === '/stock/600519' && route.name === 'stock-detail')
              ? 'bg-primary/15 text-primary-light shadow-[inset_0_0_0_1px_rgba(56,189,248,0.25)]'
              : 'text-content-muted hover:bg-base-700/60 hover:text-content'
          "
        >
          <component
            :is="item.icon"
            class="h-4 w-4 transition-transform group-hover:scale-110"
          />
          {{ item.title }}
        </router-link>
      </nav>

      <!-- 免责声明（侧栏底部常驻） -->
      <div class="border-t border-base-600/60 p-3">
        <div class="flex items-start gap-2 rounded-lg bg-warn/10 p-2.5 text-[11px] leading-relaxed text-warn">
          <ShieldAlert class="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{{ market.disclaimer }}</span>
        </div>
      </div>
    </aside>

    <!-- 主内容区（为固定侧栏留出宽度） -->
    <div class="ml-56 flex h-full flex-1 flex-col">
      <!-- 顶栏（固定） -->
      <header
        class="fixed left-56 right-0 top-0 z-30 flex h-14 items-center justify-between border-b border-base-600/60 bg-base-900/80 px-6 backdrop-blur-xl"
      >
        <div class="flex items-center gap-3">
          <h1 class="text-subheading">{{ route.meta.title ?? '大盘概览' }}</h1>
          <span
            class="flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px]"
            :class="
              market.trading
                ? 'border-up/40 bg-up/10 text-up'
                : 'border-base-600 bg-base-700/60 text-content-faint'
            "
          >
            <span
              class="h-1.5 w-1.5 rounded-full"
              :class="market.trading ? 'animate-pulse-slow bg-up' : 'bg-content-faint'"
            />
            {{ statusText }}
          </span>
        </div>
        <div class="flex items-center gap-4">
          <span class="font-num text-[13px] text-content-muted">{{ now }}</span>
          <span
            class="rounded-full border border-base-600 px-2.5 py-1 text-[11px] text-content-faint"
          >
            LLM: {{ market.llmAvailable ? market.llmModel : '规则降级' }}
          </span>
        </div>
      </header>

      <!-- 页面内容（顶栏高度补偿） -->
      <main class="flex-1 overflow-y-auto pt-14">
        <div class="mx-auto max-w-[1440px] animate-fade-in p-6">
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
