<script setup lang="ts">
/**
 * 个股详情：分时图 / K线图（可切换）+ 指标面板 + LLM 决策仪表盘。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { POLL } from '../lib/polling'
import { useRoute, useRouter } from 'vue-router'
import { ArrowLeft, CheckCircle2, AlertTriangle, XCircle, Sparkles, Star } from 'lucide-vue-next'

import {
  addWatch,
  analyzeStock,
  getIndexKline,
  getPaperTrades,
  getStockIntraday,
  getStockKline,
  getStockQuote,
  getWatchlist,
  removeWatch,
  type DecisionDashboard,
  type IntradayData,
  type PaperTrade,
  type StockQuote,
} from '../api'
import ConclusionBadge from '../components/ConclusionBadge.vue'
import IntradayChart from '../components/IntradayChart.vue'
import KLineChart from '../components/KLineChart.vue'

const route = useRoute()
const router = useRouter()
const code = computed(() => String(route.params.code ?? '600519'))

/**
 * 返回进入本页之前的页面。
 *
 * 用 history 而不是写死 `router.push('/signals')`：个股详情有四个入口
 * （信号页 / 选股中心 / 智能建议 / 模拟盘持仓），写死会把选股中心的筛选条件
 * 和模拟盘的 tab 全部丢掉。`history.state.back` 由 vue-router 维护，刷新后
 * 仍在；直接打开链接（新标签 / 收藏）时为 null，此时退回信号页兜底。
 */
function goBack() {
  const state = window.history.state as { back?: string | null } | null
  if (state?.back) {
    router.back()
    return
  }
  router.push('/signals')
}

const quote = ref<StockQuote | null>(null)
const kline = ref<{ dates: string[]; kline: number[][]; volumes: number[] } | null>(null)
const klineError = ref(false)
// 日K区间与基准：原来写死 120 天、也没有任何基准叠加，无法回答「跑赢大盘没有」
const klineDays = ref(120)
const klineRanges = [60, 120, 250] as const
const showBenchmark = ref(true)
const benchmark = ref<{ dates: string[]; closes: (number | null)[] } | null>(null)
const benchmarkError = ref(false)
const intraday = ref<IntradayData | null>(null)
const intradayError = ref(false)
const chartTab = ref<'intraday' | 'kline'>('intraday')
const chartTabs = [
  { key: 'intraday', label: '分时' },
  { key: 'kline', label: '日K' },
] as const
const dashboard = ref<DecisionDashboard | null>(null)

/** 指标明细展示顺序与格式（与后端 analyzer._build_indicators 的 key 对应）。 */
const indicatorMeta: { key: string; label: string; digits: number; suffix?: string }[] = [
  { key: 'close', label: '最新价', digits: 2 },
  { key: 'ma5', label: 'MA5', digits: 2 },
  { key: 'ma10', label: 'MA10', digits: 2 },
  { key: 'ma20', label: 'MA20', digits: 2 },
  { key: 'ma60', label: 'MA60', digits: 2 },
  { key: 'dif', label: 'DIF', digits: 3 },
  { key: 'dea', label: 'DEA', digits: 3 },
  { key: 'macd_hist', label: 'MACD柱', digits: 3 },
  { key: 'kdj_k', label: 'KDJ-K', digits: 1 },
  { key: 'kdj_d', label: 'KDJ-D', digits: 1 },
  { key: 'kdj_j', label: 'KDJ-J', digits: 1 },
  { key: 'rsi6', label: 'RSI6', digits: 1 },
  { key: 'rsi12', label: 'RSI12', digits: 1 },
  { key: 'boll_up', label: 'BOLL上', digits: 2 },
  { key: 'boll_mid', label: 'BOLL中', digits: 2 },
  { key: 'boll_dn', label: 'BOLL下', digits: 2 },
  { key: 'bias6', label: 'BIAS6', digits: 2, suffix: '%' },
  { key: 'bias12', label: 'BIAS12', digits: 2, suffix: '%' },
  { key: 'atr', label: 'ATR14', digits: 3 },
  { key: 'vol_ratio', label: '量比', digits: 2 },
  { key: 'turnover_rate', label: '换手率', digits: 2, suffix: '%' },
  { key: 'total_mv', label: '总市值', digits: 1 },
]

/** 指标数值格式化：null/NaN → '--'，否则按精度 + 后缀。 */
function fmtInd(v: number | null | undefined, m: { digits: number; suffix?: string }): string {
  if (v == null || Number.isNaN(v)) return '--'
  return v.toFixed(m.digits) + (m.suffix ?? '')
}
const analyzing = ref(false)
const inWatchlist = ref(false)     // 是否已在自选股
const watchToggling = ref(false)  // 按钮防重复点击
const watchMsg = ref('')          // 操作反馈
let pollTimer: number | undefined  // 分时轮询定时器

async function loadKline() {
  klineError.value = false
  try {
    const data = await getStockKline(code.value, klineDays.value)
    kline.value = { dates: data.dates, kline: data.kline, volumes: data.volumes }
    await loadBenchmark()
  } catch {
    // 数据源降级 / 超时：标记为错误，模板展示统一降级卡片（避免永久 loading）
    klineError.value = true
  }
}

/** 上证指数基准：多取一些天数以便按日期对齐（个股可能停牌，交易日不完全一致） */
async function loadBenchmark() {
  benchmarkError.value = false
  if (!showBenchmark.value) {
    benchmark.value = null
    return
  }
  try {
    const d = await getIndexKline('sh000001', Math.max(klineDays.value, 250))
    benchmark.value = { dates: d.dates, closes: d.closes }
  } catch {
    benchmark.value = null
    benchmarkError.value = true
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

const trades = ref<PaperTrade[]>([])

/** 加载该股票的模拟成交流水（用于分时图标注买卖点）。 */
async function loadTrades() {
  try {
    const data = await getPaperTrades(200, code.value)
    trades.value = data.trades
  } catch {
    trades.value = []
  }
}

/** 把成交映射到分时图标记：只对分时曲线所属交易日过滤（实时=今日，本地历史=对应日）。 */
const tradeMarkers = computed(() => {
  const d = intraday.value
  if (!d?.times?.length) return []
  // 实时分时接口可能不回传 date（实时源无日期列），此时曲线即当日行情，按今天过滤
  const filterDate = d.date || new Date().toISOString().slice(0, 10)
  return trades.value
    .filter((t) => t.trade_date === filterDate && (t.side === 'buy' || t.side === 'sell'))
    .map((t) => ({
      time: (t.created_at || '').split(' ')[1] || '',
      side: t.side,
      price: t.price,
      reason: t.reason,
    }))
})

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
  await Promise.all([
    loadKline(),
    loadIntraday(),
    loadQuote(),
    loadAnalysis(),
    loadWatchStatus(),
    loadTrades(),
  ])
  // 分时/报价逐分钟刷新（后端接口与前端 GET 缓存均为 10s 级，60s 足够）
  pollTimer = window.setInterval(() => {
    if (chartTab.value !== 'intraday' || document.hidden) return
    loadIntraday()
    loadQuote()
  }, POLL.slowMs)
})

onUnmounted(() => {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
})

// 同组件内切换股票（路由参数变化）时重新加载全部数据
watch(klineDays, () => { void loadKline() })
watch(showBenchmark, () => { void loadBenchmark() })

watch(code, async () => {
  watchMsg.value = ''
  await Promise.all([
    loadKline(),
    loadIntraday(),
    loadQuote(),
    loadAnalysis(),
    loadWatchStatus(),
    loadTrades(),
  ])
})

/** 分时图副标题：交易日期 + 数据来源 + 昨收基准。 */
const intradayHint = computed(() => {
  const d = intraday.value
  if (!d) return '分时走势'
  const parts = [d.date ?? '最近交易日']
  // 当日实时 / D 盘历史：让你一眼看出这根曲线是不是当天的
  if (d.source === 'realtime') parts.push('实时')
  else if (d.source === 'local') parts.push('本地历史')
  if (d.prev_close != null) parts.push(`昨收 ${d.prev_close.toFixed(2)}`)
  return parts.join(' · ')
})

const pctTone = computed(() => {
  const p = quote.value?.pct_change
  if (p === null || p === undefined || p === 0) return 'text-content'
  return p > 0 ? 'text-up' : 'text-down'
})

/**
 * 分时图的日期是否落后于日 K / 报价。
 *
 * 两条数据源不同：报价与日 K 来自日线缓存与实时快照，分时图在非交易时段读本地分钟库
 * （`minute1_fsdb`，由计划任务 SASMinutesFsdb 每日 17:30 导出）。分钟库漏一天，就会出现
 * 「页头是 10-30 收盘、分时图停在 10-29」——不提示的话极容易被当成行情接口坏了。
 *
 * 只在 `source === 'local'` 时判断：交易时段走实时源，此时日 K 缓存可能还没当天这根，
 * 拿它比较会误报。
 */
const intradayStale = computed(() => {
  const d = intraday.value
  const dates = kline.value?.dates
  const marketDate = dates && dates.length ? String(dates[dates.length - 1]) : ''
  if (!d || d.source !== 'local' || !d.date || !marketDate) return null
  if (String(d.date) >= marketDate) return null
  return { intraday: String(d.date), market: marketDate }
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
    <!-- 返回：回到进入前的页面（直达链接/新标签打开时回退到信号页） -->
    <button
      class="inline-flex items-center gap-1.5 text-[13.5px] text-content-muted transition-colors hover:text-content"
      @click="goBack"
    >
      <ArrowLeft class="h-4 w-4" />
      返回
    </button>

    <!-- 股票头部信息 -->
    <div class="glass-card flex flex-wrap items-center justify-between gap-4 p-5">
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div class="flex flex-wrap items-center gap-3 sm:gap-5">
          <div>
            <div class="flex items-center gap-2.5">
              <h2 class="text-[23.5px] font-semibold text-content">{{ quote?.name ?? code }}</h2>
              <span class="font-num rounded-md bg-base-700 px-2 py-0.5 text-[13.5px] text-content-muted">{{ code }}</span>
            </div>
            <div class="mt-2 flex items-baseline gap-3">
              <span class="font-num text-[26px] font-bold leading-none sm:text-[34px]" :class="pctTone">
                {{ quote?.price?.toFixed(2) ?? '--' }}
              </span>
              <span class="font-num text-[16.5px]" :class="pctTone">
                {{ quote?.pct_change != null ? (quote.pct_change > 0 ? '+' : '') + quote.pct_change.toFixed(2) + '%' : '--' }}
              </span>
              <span v-if="watchMsg" class="text-[12.5px] text-content-faint">{{ watchMsg }}</span>
            </div>
          </div>
        </div>
        <!-- 加入/移除自选股 -->
        <button
          class="flex items-center gap-2 rounded-lg border px-4 py-2 text-[14.5px] font-medium transition-all disabled:cursor-not-allowed disabled:opacity-50"
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
      <div class="grid grid-cols-2 gap-x-3 gap-y-2 text-[13.5px] sm:grid-cols-4 sm:gap-x-8">
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
          <div class="seg !p-0.5">
            <button
              v-for="tab in chartTabs"
              :key="tab.key"
              class="seg-item px-3 py-1"
              :class="chartTab === tab.key ? 'seg-item-active' : ''"
              @click="chartTab = tab.key"
            >
              {{ tab.label }}
            </button>
          </div>
          <div v-if="chartTab === 'kline'" class="flex flex-wrap items-center gap-2">
            <div class="seg !p-0.5" role="group" aria-label="日K区间">
              <button
                v-for="n in klineRanges"
                :key="n"
                class="seg-item px-2 py-1"
                :class="klineDays === n ? 'seg-item-active' : ''"
                @click="klineDays = n"
              >{{ n }}日</button>
            </div>
            <button
              class="btn btn-outline btn-xs"
              :class="showBenchmark ? '!border-primary/50 !text-primary-light' : ''"
              title="下方副图把个股与上证指数都归一到窗口首日 = 0%，看相对强弱"
              @click="showBenchmark = !showBenchmark"
            >{{ showBenchmark ? '基准：上证' : '基准：关' }}</button>
            <span v-if="benchmarkError" class="text-[12px] text-warn">基准数据暂不可用</span>
          </div>
          <span v-else class="text-[12.5px] text-content-faint">{{ intradayHint }}</span>
          <span
            v-if="chartTab === 'intraday' && tradeMarkers.length"
            class="flex items-center gap-2 text-[12px]"
          >
            <span class="text-up">▲ 买入</span>
            <span class="text-down">▼ 卖出</span>
          </span>
        </div>

        <!-- 分时图落后于日K/报价时明确提示（本地分钟库没导出到最新交易日） -->
        <div
          v-if="chartTab === 'intraday' && intradayStale"
          class="mb-2 flex items-start gap-2 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-[12.5px] text-warn"
        >
          <AlertTriangle class="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            分时图停在 <b>{{ intradayStale.intraday }}</b>，而日 K / 报价已是
            <b>{{ intradayStale.market }}</b>——本地分钟库没更新到这一天（不是行情接口故障）。
            跑 <code class="rounded bg-black/20 px-1">scripts\run_minutes_all.py</code> 补数据，
            或等每交易日 17:30 的计划任务 <code class="rounded bg-black/20 px-1">SASMinutesFsdb</code>。
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
            :markers="tradeMarkers"
            height="440px"
          />
          <div
            v-else-if="intradayError"
            class="flex h-[440px] flex-col items-center justify-center gap-2 text-content-faint"
          >
            <AlertTriangle class="h-8 w-8 text-warn" />
            <span class="text-[14.5px]">行情数据源暂不可用</span>
            <span class="text-[12.5px]">分时数据加载失败，可切换「日K」查看历史走势</span>
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
            :benchmark="benchmark"
            :height="showBenchmark && benchmark ? '520px' : '440px'"
          />
          <div
            v-else-if="klineError"
            class="flex h-[440px] flex-col items-center justify-center gap-2 text-content-faint"
          >
            <AlertTriangle class="h-8 w-8 text-warn" />
            <span class="text-[14.5px]">行情数据源暂不可用</span>
            <span class="text-[12.5px]">K线加载失败，请稍后重试（已尽量使用本地缓存）</span>
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
            <span class="text-[13.5px]">分析中...</span>
          </div>
        </div>

        <template v-else-if="dashboard">
          <div class="mb-4 flex flex-wrap items-center gap-3">
            <ConclusionBadge :conclusion="dashboard.conclusion" size="lg" />
            <span class="text-[12.5px] text-content-faint">
              {{ dashboard.degraded ? '规则降级' : dashboard.model }}
            </span>
            <!-- 市场情绪上下文 -->
            <span
              v-if="dashboard.sentiment?.level"
              class="rounded-full border px-2.5 py-0.5 text-[12.5px] font-medium"
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
          <p class="mb-4 text-[14.5px] leading-relaxed text-content-muted">{{ dashboard.summary }}</p>

          <!-- 三档价格 -->
          <div class="mb-4 grid grid-cols-2 gap-2 text-center xs:grid-cols-3">
            <div class="rounded-lg border border-up/30 bg-up/10 p-2">
              <div class="text-[11.5px] text-content-faint">入场价</div>
              <div class="font-num text-[16.5px] font-semibold text-up">{{ dashboard.entry_price?.toFixed(2) ?? '--' }}</div>
            </div>
            <div class="rounded-lg border border-down/30 bg-down/10 p-2">
              <div class="text-[11.5px] text-content-faint">止损价</div>
              <div class="font-num text-[16.5px] font-semibold text-down">{{ dashboard.stop_loss?.toFixed(2) ?? '--' }}</div>
            </div>
            <div class="rounded-lg border border-warn/30 bg-warn/10 p-2">
              <div class="text-[11.5px] text-content-faint">目标价</div>
              <div class="font-num text-[16.5px] font-semibold text-warn">{{ dashboard.target_price?.toFixed(2) ?? '--' }}</div>
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
                <div class="flex items-center justify-between text-[13.5px]">
                  <span class="font-medium text-content">{{ item.label }}</span>
                </div>
                <div class="text-[12.5px] text-content-faint">{{ item.detail }}</div>
              </div>
            </div>
          </div>

          <!-- 指标明细 -->
          <div v-if="dashboard.indicators" class="mt-4 border-t border-base-600/50 pt-3">
            <div class="mb-2 text-[12.5px] font-medium text-content-muted">指标明细</div>
            <div class="grid grid-cols-2 gap-x-3 gap-y-2 xs:grid-cols-3">
              <div v-for="m in indicatorMeta" :key="m.key" class="flex flex-col">
                <span class="text-[11px] text-content-faint">{{ m.label }}</span>
                <span class="font-num text-[13px] text-content">
                  {{ fmtInd(dashboard.indicators[m.key], m) }}
                </span>
              </div>
            </div>
          </div>

          <div class="mt-4 border-t border-base-600/50 pt-3 text-[11.5px] leading-relaxed text-content-faint">
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
