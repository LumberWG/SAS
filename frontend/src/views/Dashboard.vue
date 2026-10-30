<script setup lang="ts">
/**
 * 大盘概览：指数卡片 + 涨跌家数 + 成交额 + 市场温度计。
 * 30 秒自动轮询（可暂停）。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { Pause, Play, RefreshCw, Thermometer } from 'lucide-vue-next'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'

import { getAlerts, getIndexKline, getOverview, type MarketAlert, type MarketOverview } from '../api'
import { chart, tooltipStyle } from '../lib/chartTheme'
import { useMarketStore } from '../stores/market'

use([CanvasRenderer, BarChart, LineChart, GridComponent, TooltipComponent])

const market = useMarketStore()

const UP = chart.up
const DOWN = chart.down

const overview = ref<MarketOverview | null>(null)
const alerts = ref<MarketAlert[]>([])
const loading = ref(false)       // 任意加载进行中（禁用按钮防重复点击）
const spinning = ref(false)      // 按钮图标旋转（仅用户手动刷新时）
const autoRefresh = ref(true)
let timer: number | undefined

// 指数迷你走势（收盘价序列）
const sparklines = reactive<Record<string, (number | null)[]>>({})
const sparkReady = ref(false)   // 迷你走势是否已加载（60天走势无需随轮询重复拉取）

async function load(force = false) {
  loading.value = true
  if (force) spinning.value = true
  try {
    overview.value = await getOverview()
    market.trading = overview.value.trading
    // 迷你走势：仅首次或用户手动刷新时拉取（后端已加 Parquet 缓存，
    // 但指数K线首次要走完整降级链较慢，不应随 30s 轮询重复请求）
    if (force || !sparkReady.value) {
      // 并发拉取前 4 个指数迷你走势，避免顺序 await 叠加等待（单接口慢时
      // 串行会放大到数倍耗时）；单个失败不影响其余与页面渲染。
      const syms = (overview.value.indices || []).slice(0, 4).map((i) => i.symbol)
      const results = await Promise.all(
        syms.map((sym) =>
          getIndexKline(sym, 60)
            .then((d) => [sym, d.closes] as const)
            .catch(() => [sym, [] as (number | null)[]] as const),
        ),
      )
      for (const [sym, closes] of results) sparklines[sym] = closes
      sparkReady.value = true
    }
    // 拉取最近异动（调度器检测结果，轻量）
    try {
      const alertData = await getAlerts(20)
      alerts.value = alertData.alerts
    } catch {
      alerts.value = []
    }
  } finally {
    loading.value = false
    spinning.value = false
  }
}

function alertTone(type: string): string {
  if (type === '涨停' || type === '急涨') return 'text-up'
  if (type === '跌停' || type === '急跌') return 'text-down'
  return 'text-warn'
}

function alertBadgeClass(type: string): string {
  if (type === '涨停' || type === '急涨') return 'border-up/40 bg-up/10 text-up'
  if (type === '跌停' || type === '急跌') return 'border-down/40 bg-down/10 text-down'
  return 'border-warn/40 bg-warn/10 text-warn'
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    timer = window.setInterval(() => load(), 30000)
  } else if (timer) {
    window.clearInterval(timer)
  }
}

onMounted(() => {
  load()
  if (autoRefresh.value) {
    timer = window.setInterval(() => load(), 30000)
  }
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})

// ---------------- 格式化 ----------------
function fmtAmount(yuan: number): string {
  if (yuan >= 1e12) return `${(yuan / 1e12).toFixed(2)} 万亿`
  if (yuan >= 1e8) return `${(yuan / 1e8).toFixed(0)} 亿`
  return `${(yuan / 1e4).toFixed(0)} 万`
}

function fmtPct(pct: number | null): string {
  return pct === null ? '--' : `${pct > 0 ? '+' : ''}${pct.toFixed(2)}%`
}

function tone(pct: number | null): 'up' | 'down' | 'neutral' {
  if (pct === null || pct === 0) return 'neutral'
  return pct > 0 ? 'up' : 'down'
}

function tempLabel(temp: number): string {
  if (temp >= 70) return '偏热'
  if (temp >= 55) return '偏强'
  if (temp >= 45) return '中性'
  if (temp >= 30) return '偏弱'
  return '偏冷'
}

// 情绪权重配色: 正权重=多头环境(红), 负=空头环境(绿), 0=中性
function weightTone(weight: number): string {
  if (weight > 0) return 'text-up'
  if (weight < 0) return 'text-down'
  return 'text-content'
}

function weightBorder(weight: number): string {
  if (weight > 0) return 'border-up/40 bg-up/10'
  if (weight < 0) return 'border-down/40 bg-down/10'
  return 'border-base-600 bg-base-700/60'
}

// 温度环颜色跟随情绪权重
const sentimentStroke = computed(() => {
  const w = overview.value?.sentiment?.weight ?? 0
  if (w > 0) return UP
  if (w < 0) return DOWN
  return chart.textFaint
})

// 迷你走势 option
function sparkOption(data: (number | null)[]) {
  return {
    animation: false,
    grid: { left: 0, right: 0, top: 4, bottom: 0 },
    xAxis: { type: 'category', show: false },
    yAxis: { type: 'value', show: false, min: (v: { min: number }) => v.min * 0.998 },
    series: [
      {
        type: 'line',
        data,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.5, color: chart.primaryLight },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: `${chart.primaryLight}40` },
              { offset: 1, color: `${chart.primaryLight}00` },
            ],
          },
        },
      },
    ],
  }
}

// 涨跌家数条形图 option
const breadthOption = computed(() => {
  const b = overview.value?.breadth
  if (!b) return {}
  return {
    animation: false,
    grid: { left: 46, right: 34, top: 6, bottom: 4 },
    tooltip: {
      trigger: 'axis',
      ...tooltipStyle,
    },
    xAxis: {
      type: 'value',
      axisLabel: { color: chart.textFaint, fontSize: 10 },
      splitLine: { lineStyle: { color: chart.splitLine } },
    },
    yAxis: {
      type: 'category',
      data: ['跌停', '下跌', '平盘', '上涨', '涨停'],
      axisLabel: { color: chart.textMuted, fontSize: 10 },
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar',
        data: [
          { value: b.limit_down, itemStyle: { color: DOWN } },
          { value: b.down, itemStyle: { color: `${chart.down}8C` } },
          { value: b.flat, itemStyle: { color: chart.textFaint } },
          { value: b.up, itemStyle: { color: `${chart.up}8C` } },
          { value: b.limit_up, itemStyle: { color: UP } },
        ],
        barWidth: 10,
        label: { show: true, position: 'right', color: chart.text, fontSize: 10 },
      },
    ],
  }
})
</script>

<template>
  <div class="space-y-3">
    <!-- 操作栏 -->
    <div class="flex items-center justify-between">
      <div class="flex items-center gap-2 text-[12px] text-content-faint">
        <span v-if="overview">数据更新于 {{ overview.updated_at }}</span>
      </div>
      <div class="flex items-center gap-2">
        <button class="btn btn-outline" @click="toggleAuto">
          <Pause v-if="autoRefresh" class="h-3.5 w-3.5" />
          <Play v-else class="h-3.5 w-3.5" />
          {{ autoRefresh ? '暂停轮询' : '开启轮询' }}
        </button>
        <button class="btn btn-outline" :disabled="loading" @click="load(true)">
          <RefreshCw class="h-3.5 w-3.5" :class="spinning ? 'animate-spin' : ''" />
          刷新
        </button>
      </div>
    </div>

    <!-- 指数卡片：一行铺开 6 个，只留价格/涨跌/成交额，迷你走势压到 40px -->
    <div class="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
      <div
        v-for="idx in overview?.indices ?? []"
        :key="idx.symbol"
        class="glass-card px-3 py-2"
      >
        <div class="flex items-center justify-between gap-1">
          <span class="truncate text-[11px] text-content-muted">{{ idx.name }}</span>
          <span class="font-num shrink-0 text-[10px] text-content-faint">
            {{ fmtAmount(idx.amount ?? 0) }}
          </span>
        </div>
        <div class="mt-0.5 flex items-baseline gap-1.5">
          <span
            class="font-num text-[19px] font-semibold leading-none"
            :class="{
              'text-up': tone(idx.pct_change) === 'up',
              'text-down': tone(idx.pct_change) === 'down',
              'text-content': tone(idx.pct_change) === 'neutral',
            }"
          >
            {{ idx.price?.toFixed(2) ?? '--' }}
          </span>
          <span
            class="font-num text-[11px]"
            :class="{
              'text-up': tone(idx.pct_change) === 'up',
              'text-down': tone(idx.pct_change) === 'down',
              'text-content-faint': tone(idx.pct_change) === 'neutral',
            }"
          >
            {{ fmtPct(idx.pct_change) }}
          </span>
        </div>
        <div class="mt-0.5 h-8">
          <v-chart :option="sparkOption(sparklines[idx.symbol] ?? [])" style="height: 32px" autoresize />
        </div>
      </div>
    </div>

    <!-- 市场宽度 + 情绪 + 成交额：三块并排，压到同一行的高度 -->
    <div class="grid grid-cols-1 gap-2 lg:grid-cols-4">
      <!-- 涨跌家数 -->
      <div class="glass-card px-3 py-2 lg:col-span-2">
        <div class="flex items-center justify-between">
          <h3 class="text-[13px] font-medium text-content">涨跌家数</h3>
          <span class="text-[11px] text-content-faint">
            共 {{ overview?.breadth.total ?? 0 }} 只
            <template v-if="(overview?.breadth.untraded ?? 0) > 0">
              （未成交 {{ overview?.breadth.untraded }}）
            </template>
          </span>
        </div>
        <v-chart v-if="overview" :option="breadthOption" style="height: 132px" autoresize />
      </div>

      <!-- 市场温度 + 情绪等级 -->
      <div class="glass-card flex items-center gap-3 px-3 py-2">
        <div class="relative flex h-20 w-20 shrink-0 items-center justify-center">
          <svg viewBox="0 0 120 120" class="h-full w-full -rotate-90">
            <circle cx="60" cy="60" r="52" fill="none" stroke-width="10" class="stroke-base-700" />
            <circle
              cx="60"
              cy="60"
              r="52"
              fill="none"
              :stroke="sentimentStroke"
              stroke-width="10"
              stroke-linecap="round"
              :stroke-dasharray="`${((overview?.temperature ?? 50) / 100) * 327} 327`"
              class="transition-all duration-700"
            />
          </svg>
          <div class="absolute flex flex-col items-center">
            <span class="font-num text-[18px] font-semibold leading-none text-content">
              {{ overview?.temperature?.toFixed(1) ?? '--' }}
            </span>
            <span class="text-[10px] text-content-muted">{{ tempLabel(overview?.temperature ?? 50) }}</span>
          </div>
        </div>
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1.5 text-content-muted">
            <Thermometer class="h-3.5 w-3.5" />
            <span class="text-[12px]">市场情绪</span>
          </div>
          <div v-if="overview?.sentiment" class="mt-1">
            <span
              class="inline-block rounded-full border px-2 py-0.5 text-[11px] font-semibold"
              :class="weightBorder(overview.sentiment.weight)"
            >
              {{ overview.sentiment.level }}
              <span class="font-num ml-1" :class="weightTone(overview.sentiment.weight)">
                {{ overview.sentiment.weight > 0 ? '+' : '' }}{{ overview.sentiment.weight }}
              </span>
            </span>
            <p class="mt-1 line-clamp-2 text-[10px] leading-snug text-content-faint">
              {{ overview.sentiment.advice }}
            </p>
          </div>
          <div class="mt-1 flex gap-3 font-num text-[11px]">
            <span class="text-up">涨停 {{ overview?.breadth.limit_up ?? 0 }}</span>
            <span class="text-down">跌停 {{ overview?.breadth.limit_down ?? 0 }}</span>
          </div>
        </div>
      </div>

      <!-- 成交额汇总 -->
      <div class="glass-card flex flex-col justify-center px-3 py-2">
        <div class="text-[11px] text-content-muted">全市场成交额</div>
        <div class="font-num text-[20px] font-semibold leading-tight text-content">
          {{ fmtAmount(overview?.total_amount ?? 0) }}
        </div>
        <div class="font-num text-[10px] leading-snug text-content-faint">
          上涨 {{ overview?.breadth.up ?? 0 }} · 下跌 {{ overview?.breadth.down ?? 0 }} · 平盘
          {{ overview?.breadth.flat ?? 0 }}
        </div>
      </div>
    </div>

    <!-- 异动提醒（调度器实时检测，新→旧）：一屏内多列铺开，限高滚动 -->
    <div class="glass-card px-3 py-2">
      <div class="mb-1.5 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <h3 class="text-[13px] font-medium text-content">异动提醒</h3>
          <span class="rounded border border-base-600 px-1.5 py-0.5 text-[10px] text-content-faint">
            涨跌停 / 放量突破 / 急涨急跌
          </span>
        </div>
        <span class="text-[11px] text-content-faint">最近 {{ alerts.length }} 条</span>
      </div>

      <div
        v-if="alerts.length > 0"
        class="grid max-h-[220px] grid-cols-1 gap-1.5 overflow-y-auto pr-1 md:grid-cols-2 xl:grid-cols-4"
      >
        <div
          v-for="(alert, i) in alerts"
          :key="i"
          class="flex items-center justify-between gap-2 rounded border border-base-600/50 bg-base-700/30 px-2 py-1 transition-colors hover:border-primary/40"
        >
          <div class="flex min-w-0 items-center gap-1.5">
            <span class="shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-medium" :class="alertBadgeClass(alert.type)">
              {{ alert.type }}
            </span>
            <div class="min-w-0">
              <div class="truncate text-[12px] font-medium text-content">{{ alert.name }}</div>
              <div class="font-num truncate text-[10px] text-content-faint">
                {{ alert.code }}<span v-if="alert.volume_ratio"> · 量比 {{ alert.volume_ratio.toFixed(1) }}</span>
              </div>
            </div>
          </div>
          <div class="shrink-0 text-right">
            <div class="font-num text-[13px] font-semibold" :class="alertTone(alert.type)">
              {{ alert.pct_change > 0 ? '+' : '' }}{{ alert.pct_change.toFixed(2) }}%
            </div>
            <div class="font-num text-[10px] text-content-faint">{{ alert.time }}</div>
          </div>
        </div>
      </div>

      <div v-else class="flex flex-col items-center gap-1 py-6 text-content-faint">
        <p class="text-[12px]">暂无异动记录</p>
        <p class="text-[11px]">调度器每 5 分钟检测一次（需后端运行且处于数据可达网络）</p>
      </div>
    </div>
  </div>
</template>
