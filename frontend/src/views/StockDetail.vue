<script setup lang="ts">
/**
 * 个股详情：分时图 / K线图（可切换）+ 指标面板 + LLM 决策仪表盘。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { CheckCircle2, AlertTriangle, XCircle, Sparkles, Star } from 'lucide-vue-next'

import {
  addWatch,
  analyzeStock,
  getStockIntraday,
  getStockKline,
  getStockQuote,
  getWatchlist,
  removeWatch,
  type DecisionDashboard,
  type IntradayData,
  type StockQuote,
} from '../api'
import ConclusionBadge from '../components/ConclusionBadge.vue'
import IntradayChart from '../components/IntradayChart.vue'
import KLineChart from '../components/KLineChart.vue'

const route = useRoute()
const code = computed(() => String(route.params.code ?? '600519'))

const quote = ref<StockQuote | null>(null)
const kline = ref<{ dates: string[]; kline: number[][]; volumes: number[] } | null>(null)
const klineError = ref(false)
const intraday = ref<IntradayData | null>(null)
const intradayError = ref(false)
const chartTab = ref<'intraday' | 'kline'>('intraday')
const chartTabs = [
  { key: 'intraday', label: '分时' },
  { key: 'kline', label: '日K' },
] as const
const dashboard = ref<DecisionDashboard | null>(null)
const analyzing = ref(false)
const inWatchlist = ref(false)     // 是否已在自选股
const watchToggling = ref(false)  // 按钮防重复点击
const watchMsg = ref('')          // 操作反馈
let pollTimer: number | undefined  // 分时轮询定时器

async function loadKline() {
  klineError.value = false
  try {
    const data = await getStockKline(code.value, 120)
    kline.value = { dates: data.dates, kline: data.kline, volumes: data.volumes }
  } catch {
    // 数据源降级 / 超时：标记为错误，模板展示统一降级卡片（避免永久 loading）
    klineError.value = true
  }
}

async function loadIntraday() {
  intradayError.value = false
  try {
    const data = await getStockIntraday(code.value)
    // 防御：后端未升级时该路径会落到 SPA 回退返回 index.html（200 + HTML）
    if (!Array.isArray(data?.times)) throw new Error('invalid intraday payload')
    intraday.value = data
  } catch {
    // 与K线一致：数据源降级/超时时走模板里的统一降级卡片，避免永久 loading
    intradayError.value = true
  }
}

async function loadQuote() {
  try {
    quote.value = await getStockQuote(code.value)
  } catch {
    quote.value = null
  }
}

async function loadAnalysis() {
  analyzing.value = true
  try {
    dashboard.value = await analyzeStock(code.value)
  } catch {
    dashboard.value = null
  } finally {
    analyzing.value = false
  }
}

/** 查询当前股票是否已在自选股（按钮状态）。 */
async function loadWatchStatus() {
  try {
    const { items } = await getWatchlist()
    inWatchlist.value = items.some((w) => w.code === code.value)
  } catch {
    inWatchlist.value = false
  }
}

/** 加入/移除自选股。 */
async function toggleWatch() {
  watchToggling.value = true
  watchMsg.value = ''
  try {
    if (inWatchlist.value) {
      await removeWatch(code.value)
      inWatchlist.value = false
      watchMsg.value = '已移除自选'
    } else {
      await addWatch(code.value)
      inWatchlist.value = true
      watchMsg.value = '已加入自选'
    }
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response
      ?.data?.detail
    // 409 = 已在自选股中，同步按钮状态
    if ((e as { response?: { status?: number } })?.response?.status === 409) {
      inWatchlist.value = true
    }
    watchMsg.value = detail ?? '操作失败'
  } finally {
    watchToggling.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadKline(), loadIntraday(), loadQuote(), loadAnalysis(), loadWatchStatus()])
  // 分时/报价逐分钟刷新（后端接口与前端 GET 缓存均为 10s 级，60s 足够）
  pollTimer = window.setInterval(() => {
    if (chartTab.value !== 'intraday' || document.hidden) return
    loadIntraday()
    loadQuote()
  }, 60_000)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
})

// 同组件内切换股票（路由参数变化）时重新加载全部数据
watch(code, async () => {
  watchMsg.value = ''
  await Promise.all([loadKline(), loadIntraday(), loadQuote(), loadAnalysis(), loadWatchStatus()])
})

/** 分时图副标题：交易日期 + 昨收基准。 */
const intradayHint = computed(() => {
  const d = intraday.value
  if (!d) return '分时走势'
  const parts = [d.date ?? '最近交易日']
  if (d.prev_close != null) parts.push(`昨收 ${d.prev_close.toFixed(2)}`)
  return parts.join(' · ')
})

const pctTone = computed(() => {
  const p = quote.value?.pct_change
  if (p === null || p === undefined || p === 0) return 'text-content'
  return p > 0 ? 'text-up' : 'text-down'
})

function statusIcon(status: string) {
  if (status === 'pass') return CheckCircle2
  if (status === 'fail') return XCircle
  return AlertTriangle
}

function statusColor(status: string) {
  if (status === 'pass') return 'text-up'
  if (status === 'fail') return 'text-down'
  return 'text-warn'
}
</script>

<template>
  <div class="space-y-5">
    <!-- 股票头部信息 -->
    <div class="glass-card flex flex-wrap items-center justify-between gap-4 p-5">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div class="flex items-center gap-5">
          <div>
            <div class="flex items-center gap-2.5">
              <h2 class="text-[22px] font-semibold text-content">{{ quote?.name ?? code }}</h2>
              <span class="font-num rounded-md bg-base-700 px-2 py-0.5 text-[12px] text-content-muted">{{ code }}</span>
            </div>
            <div class="mt-2 flex items-baseline gap-3">
              <span class="font-num text-[32px] font-bold leading-none" :class="pctTone">
                {{ quote?.price?.toFixed(2) ?? '--' }}
              </span>
              <span class="font-num text-[15px]" :class="pctTone">
                {{ quote?.pct_change != null ? (quote.pct_change > 0 ? '+' : '') + quote.pct_change.toFixed(2) + '%' : '--' }}
              </span>
              <span v-if="watchMsg" class="text-[11px] text-content-faint">{{ watchMsg }}</span>
            </div>
          </div>
        </div>
        <!-- 加入/移除自选股 -->
        <button
          class="flex items-center gap-2 rounded-lg border px-4 py-2 text-[13px] font-medium transition-all disabled:cursor-not-allowed disabled:opacity-50"
          :class="
            inWatchlist
              ? 'border-warn/50 bg-warn/10 text-warn hover:bg-warn/20'
              : 'border-base-600 text-content-muted hover:border-warn/50 hover:text-warn'
          "
          :disabled="watchToggling"
          @click="toggleWatch"
        >
          <Star class="h-4 w-4" :class="inWatchlist ? 'fill-warn' : ''" />
          {{ inWatchlist ? '已自选' : '加入自选' }}
        </button>
      </div>
      <div class="grid grid-cols-2 gap-x-8 gap-y-2 text-[12px] sm:grid-cols-4">
        <div><div class="text-content-faint">今开</div><div class="font-num text-content">{{ quote?.open?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">最高</div><div class="font-num text-up">{{ quote?.high?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">最低</div><div class="font-num text-down">{{ quote?.low?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">昨收</div><div class="font-num text-content">{{ quote?.prev_close?.toFixed(2) ?? '--' }}</div></div>
        <div><div class="text-content-faint">成交量</div><div class="font-num text-content">{{ quote?.volume ? (quote.volume / 1e4).toFixed(1) + ' 万手' : '--' }}</div></div>
        <div><div class="text-content-faint">成交额</div><div class="font-num text-content">{{ quote?.amount ? (quote.amount / 1e8).toFixed(2) + ' 亿' : '--' }}</div></div>
        <div><div class="text-content-faint">换手率</div><div class="font-num text-content">{{ quote?.turnover_rate != null ? quote.turnover_rate.toFixed(2) + '%' : '--' }}</div></div>
        <div><div class="text-content-faint">量比</div><div class="font-num text-content">{{ quote?.volume_ratio?.toFixed(2) ?? '--' }}</div></div>
      </div>
    </div>

    <div class="grid grid-cols-1 gap-5 xl:grid-cols-4">
      <!-- 分时 / 日K 走势 -->
      <div class="glass-card p-4 xl:col-span-3">
        <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div class="flex items-center gap-1 rounded-lg border border-base-600 bg-base-700/40 p-0.5">
            <button
              v-for="tab in chartTabs"
              :key="tab.key"
              class="rounded-md px-3 py-1 text-[12px] font-medium transition-colors"
              :class="
                chartTab === tab.key
                  ? 'bg-base-600 text-content'
                  : 'text-content-muted hover:text-content'
              "
              @click="chartTab = tab.key"
            >
              {{ tab.label }}
            </button>
          </div>
          <span class="text-[11px] text-content-faint">
            {{ chartTab === 'intraday' ? intradayHint : 'MA5 天蓝 · MA10 黄 · MA20 紫' }}
          </span>
        </div>

        <!-- 分时图 -->
        <template v-if="chartTab === 'intraday'">
          <IntradayChart
            v-if="intraday"
            :times="intraday.times"
            :prices="intraday.prices"
            :avg-prices="intraday.avg_prices"
            :volumes="intraday.volumes"
            :prev-close="intraday.prev_close"
            height="440px"
          />
          <div
            v-else-if="intradayError"
            class="flex h-[440px] flex-col items-center justify-center gap-2 text-content-faint"
          >
            <AlertTriangle class="h-8 w-8 text-warn" />
            <span class="text-[13px]">行情数据源暂不可用</span>
            <span class="text-[11px]">分时数据加载失败，可切换「日K」查看历史走势</span>
          </div>
          <div v-else class="flex h-[440px] items-center justify-center text-content-faint">
            分时加载中...
          </div>
        </template>

        <!-- 日K线 -->
        <template v-else>
          <KLineChart
            v-if="kline"
            :dates="kline.dates"
            :kline="kline.kline"
            :volumes="kline.volumes"
            height="440px"
          />
          <div
            v-else-if="klineError"
            class="flex h-[440px] flex-col items-center justify-center gap-2 text-content-faint"
          >
            <AlertTriangle class="h-8 w-8 text-warn" />
            <span class="text-[13px]">行情数据源暂不可用</span>
            <span class="text-[11px]">K线加载失败，请稍后重试（已尽量使用本地缓存）</span>
          </div>
          <div v-else class="flex h-[440px] items-center justify-center text-content-faint">
            K线加载中...
          </div>
        </template>
      </div>

      <!-- LLM 决策仪表盘 -->
      <div class="glass-card flex flex-col p-5">
        <div class="mb-3 flex items-center gap-2">
          <Sparkles class="h-4 w-4 text-primary-light" />
          <h3 class="text-subheading">AI 决策仪表盘</h3>
        </div>

        <div v-if="analyzing" class="flex flex-1 items-center justify-center text-content-faint">
          <div class="flex flex-col items-center gap-2">
            <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary-light border-t-transparent" />
            <span class="text-[12px]">分析中...</span>
          </div>
        </div>

        <template v-else-if="dashboard">
          <div class="mb-4 flex flex-wrap items-center gap-3">
            <ConclusionBadge :conclusion="dashboard.conclusion" size="lg" />
            <span class="text-[11px] text-content-faint">
              {{ dashboard.degraded ? '规则降级' : dashboard.model }}
            </span>
            <!-- 市场情绪上下文 -->
            <span
              v-if="dashboard.sentiment?.level"
              class="rounded-full border px-2.5 py-0.5 text-[11px] font-medium"
              :class="
                dashboard.sentiment.weight > 0
                  ? 'border-up/40 bg-up/10 text-up'
                  : dashboard.sentiment.weight < 0
                    ? 'border-down/40 bg-down/10 text-down'
                    : 'border-base-600 bg-base-700/60 text-content-muted'
              "
              :title="dashboard.sentiment.advice"
            >
              {{ dashboard.sentiment.level }}
              {{ dashboard.sentiment.weight > 0 ? '+' : '' }}{{ dashboard.sentiment.weight }}
            </span>
          </div>
          <p class="mb-4 text-[13px] leading-relaxed text-content-muted">{{ dashboard.summary }}</p>

          <!-- 三档价格 -->
          <div class="mb-4 grid grid-cols-3 gap-2 text-center">
            <div class="rounded-lg border border-up/30 bg-up/10 p-2">
              <div class="text-[10px] text-content-faint">入场价</div>
              <div class="font-num text-[15px] font-semibold text-up">{{ dashboard.entry_price?.toFixed(2) ?? '--' }}</div>
            </div>
            <div class="rounded-lg border border-down/30 bg-down/10 p-2">
              <div class="text-[10px] text-content-faint">止损价</div>
              <div class="font-num text-[15px] font-semibold text-down">{{ dashboard.stop_loss?.toFixed(2) ?? '--' }}</div>
            </div>
            <div class="rounded-lg border border-warn/30 bg-warn/10 p-2">
              <div class="text-[10px] text-content-faint">目标价</div>
              <div class="font-num text-[15px] font-semibold text-warn">{{ dashboard.target_price?.toFixed(2) ?? '--' }}</div>
            </div>
          </div>

          <!-- 检查清单 -->
          <div class="space-y-2">
            <div
              v-for="item in dashboard.checklist"
              :key="item.label"
              class="flex items-start gap-2 rounded-lg bg-base-700/40 p-2"
            >
              <component :is="statusIcon(item.status)" class="mt-0.5 h-3.5 w-3.5 shrink-0" :class="statusColor(item.status)" />
              <div class="min-w-0 flex-1">
                <div class="flex items-center justify-between text-[12px]">
                  <span class="font-medium text-content">{{ item.label }}</span>
                </div>
                <div class="text-[11px] text-content-faint">{{ item.detail }}</div>
              </div>
            </div>
          </div>

          <div class="mt-4 border-t border-base-600/50 pt-3 text-[10px] leading-relaxed text-content-faint">
            {{ dashboard.generated_at }} 生成 · 仅供学习研究，不构成投资建议
          </div>
        </template>

        <div v-else class="flex flex-1 items-center justify-center text-content-faint">
          分析暂不可用
        </div>
      </div>
    </div>
  </div>
</template>
