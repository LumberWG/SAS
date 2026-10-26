<script setup lang="ts">
/**
 * 模拟交易：10万模拟盘实时撮合系统买卖建议，验证选股思路准确性。
 * 数据：账户/持仓/流水/净值/实时引擎状态（15s 轮询）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowDownCircle,
  ArrowUpCircle,
  RefreshCw,
  RotateCcw,
  Wallet,
  Zap,
} from 'lucide-vue-next'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'

import {
  getPaperAccount,
  getPaperEquity,
  getPaperLive,
  getPaperTrades,
  getWatchlist,
  placePaperOrder,
  resetPaperAccount,
  setPaperMode,
  tickPaper,
  type PaperAccount,
  type PaperLiveStatus,
  type PaperTrade,
  type WatchItem,
} from '../api'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])

const router = useRouter()

const account = ref<PaperAccount | null>(null)
const trades = ref<PaperTrade[]>([])
const equity = ref<Array<{ date: string; total_asset: number }>>([])
const live = ref<PaperLiveStatus | null>(null)
const resetting = ref(false)
const ticking = ref(false)
const switching = ref(false)
let timer: number | undefined

// ---- 手动下单 ----
const watchItems = ref<WatchItem[]>([])
const order = ref({ code: '', side: 'buy' as 'buy' | 'sell', amount: '', shares: '' })
const ordering = ref(false)
const orderMsg = ref('')
const orderOk = ref(false)

async function load() {
  try {
    const [acc, tradeData, equityData, liveData] = await Promise.all([
      getPaperAccount(),
      getPaperTrades(100),
      getPaperEquity(),
      getPaperLive(),
    ])
    account.value = acc
    trades.value = tradeData.trades
    equity.value = equityData.equity.map((e) => ({
      date: e.date.slice(5),
      total_asset: e.total_asset,
    }))
    live.value = liveData
    // 手动模式下列表里的现价用于下单参考，随轮询一起刷新
    if (!liveData.auto_trade) loadWatchItems()
  } catch {
    // 保留旧数据
  }
}

/** 立即撮合一次（调试用：不必等 5s 周期，也不会重复下单） */
async function tickNow() {
  ticking.value = true
  try {
    await tickPaper()
    await load()
  } catch {
    // 静默：状态条会展示 last_error
  } finally {
    ticking.value = false
  }
}

/** 切换自动/手动交易模式（持久化到后端） */
async function toggleMode(auto: boolean) {
  if (switching.value || !live.value || live.value.auto_trade === auto) return
  const tip = auto
    ? '切换到自动交易？系统将按建议实时撮合并执行盘中止盈止损，人工不再干预。'
    : '切换到手动交易？自动撮合与止盈止损会暂停，买卖由你手动下单。'
  if (!confirm(tip)) return
  switching.value = true
  try {
    live.value = await setPaperMode(auto)
    if (!auto) await loadWatchItems()
    await load()
  } catch {
    // 静默：下次轮询会同步真实状态
  } finally {
    switching.value = false
  }
}

async function loadWatchItems() {
  try {
    const { items } = await getWatchlist()
    watchItems.value = items
  } catch {
    // 静默：下拉为空时仍可输入代码
  }
}

/** 下单候选：自选 + 持仓（去重，持仓在前） */
const tradeOptions = computed(() => {
  const map = new Map<string, string>()
  for (const p of account.value?.positions ?? []) map.set(p.code, p.name)
  for (const w of watchItems.value) if (!map.has(w.code)) map.set(w.code, w.name)
  return [...map.entries()].map(([code, name]) => ({ code, name }))
})

/** 手动下单参考价：撮合引擎最新报价 → 持仓现价 → 自选行情 */
const orderPrice = computed<number | null>(() => {
  const code = order.value.code
  if (!code) return null
  const tick = live.value?.prices?.[code]
  if (tick) return tick
  const pos = account.value?.positions.find((p) => p.code === code)
  if (pos?.price) return pos.price
  return watchItems.value.find((w) => w.code === code)?.price ?? null
})

async function submitOrder() {
  const code = order.value.code.trim()
  if (!code) {
    orderOk.value = false
    orderMsg.value = '请选择股票'
    return
  }
  ordering.value = true
  orderMsg.value = ''
  try {
    const payload = {
      code,
      side: order.value.side,
      name: tradeOptions.value.find((o) => o.code === code)?.name ?? '',
    } as { code: string; side: 'buy' | 'sell'; amount?: number; shares?: number; name?: string }
    if (order.value.side === 'buy') {
      const amount = Number(order.value.amount)
      if (!amount || amount <= 0) throw new Error('请输入买入金额')
      payload.amount = amount
    } else {
      const shares = Number(order.value.shares)
      if (order.value.shares && (!shares || shares <= 0)) throw new Error('股数需为正整数')
      if (shares > 0) payload.shares = shares
    }
    const res = await placePaperOrder(payload)
    const r = res.result
    orderOk.value = r.side !== 'skip'
    orderMsg.value = orderOk.value
      ? `${r.side === 'buy' ? '买入' : '卖出'} ${r.name} ${r.shares} 股 @ ${r.price.toFixed(2)}，费用 ${r.fee.toFixed(2)} 元`
      : `未成交：${r.reason}`
    account.value = res.account
    if (orderOk.value) order.value.shares = ''
    await load()
  } catch (err) {
    orderOk.value = false
    orderMsg.value = (err as Error).message || '下单失败'
  } finally {
    ordering.value = false
  }
}

async function reset() {
  if (!confirm('确定重置模拟账户？将清空全部持仓与交易记录，资金恢复 10 万。')) return
  resetting.value = true
  try {
    await resetPaperAccount()
    await load()
  } finally {
    resetting.value = false
  }
}

// 净值曲线 option（初始资金基准线）
const equityOption = computed(() => {
  const initial = account.value?.initial_capital ?? 100000
  return {
    animation: false,
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#111A2E',
      borderColor: '#243354',
      textStyle: { color: '#E6EDF7', fontSize: 12 },
      valueFormatter: (v: number) => `${v.toFixed(0)} 元`,
    },
    xAxis: {
      type: 'category',
      data: equity.value.map((e) => e.date),
      axisLabel: { color: '#5C6B85', fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: '#5C6B85', fontSize: 10 },
      splitLine: { lineStyle: { color: 'rgba(36,51,84,0.6)' } },
    },
    series: [
      {
        type: 'line',
        data: equity.value.map((e) => e.total_asset),
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2, color: '#38BDF8' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(56,189,248,0.25)' },
              { offset: 1, color: 'rgba(56,189,248,0)' },
            ],
          },
        },
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: '#5C6B85', type: 'dashed' },
          data: [{ yAxis: initial, label: { show: true, position: 'end', formatter: '初始10万', color: '#5C6B85', fontSize: 10 } }],
        },
      },
    ],
  }
})

function pctTone(pct: number): string {
  return pct > 0 ? 'text-up' : pct < 0 ? 'text-down' : 'text-content'
}

const liveStatusText = computed(() => {
  if (!live.value) return '实时撮合状态获取中'
  if (!live.value.engine_enabled) return '实时撮合引擎已关闭'
  if (!live.value.auto_trade) return '手动交易模式 · 自动撮合已暂停'
  return live.value.trading ? '自动交易中 · 人工不干预' : '待开盘 · 信号排队中'
})

onMounted(() => {
  load()
  loadWatchItems()
  timer = window.setInterval(load, 15000)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="space-y-5">
    <!-- 账户总览 -->
    <div class="glass-card flex flex-wrap items-center justify-between gap-4 p-5">
      <div class="flex items-center gap-3">
        <div class="flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-primary-light shadow-glow">
          <Wallet class="h-5 w-5 text-white" />
        </div>
        <div>
          <h2 class="text-[17px] font-semibold text-content">模拟交易</h2>
          <p class="mt-0.5 text-[11px] text-content-faint">
            建议实时撮合 · 等分仓位最多10只 · 整手 · T+1 · 佣金万2.5+印花税千1
          </p>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <div class="rounded-lg border border-base-600 bg-base-700/40 px-4 py-2 text-center">
          <div
            class="font-num text-[22px] font-bold leading-none"
            :class="pctTone(account?.return_pct ?? 0)"
          >
            {{ account ? (account.return_pct > 0 ? '+' : '') + account.return_pct.toFixed(2) + '%' : '--' }}
          </div>
          <div class="mt-1 text-[11px] text-content-faint">累计收益率</div>
        </div>
        <div class="rounded-lg border border-base-600 bg-base-700/40 px-4 py-2 text-center">
          <div class="font-num text-[22px] font-bold leading-none text-content">
            {{ account ? (account.total_asset / 10000).toFixed(2) + ' 万' : '--' }}
          </div>
          <div class="mt-1 text-[11px] text-content-faint">总资产</div>
        </div>
        <button
          class="flex items-center gap-1.5 rounded-lg border border-base-600 px-3 py-2 text-[12px] text-content-muted transition-colors hover:border-down/50 hover:text-down disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="resetting"
          @click="reset"
        >
          <RotateCcw class="h-3.5 w-3.5" />
          重置账户
        </button>
      </div>
    </div>

    <!-- 交易模式 / 实时撮合状态 -->
    <div class="glass-card flex flex-wrap items-center gap-x-6 gap-y-3 p-4">
      <div class="flex items-center gap-2">
        <span class="relative flex h-2 w-2">
          <span
            class="absolute inline-flex h-full w-full rounded-full opacity-60"
            :class="live?.auto_trade && live?.trading ? 'animate-ping bg-up' : 'bg-content-faint'"
          />
          <span
            class="relative inline-flex h-2 w-2 rounded-full"
            :class="live?.auto_trade && live?.trading ? 'bg-up' : 'bg-content-faint'"
          />
        </span>
        <span class="text-[13px] font-medium text-content">{{ liveStatusText }}</span>
      </div>
      <span class="text-[11px] text-content-faint">
        每 {{ live?.tick_interval ?? 5 }}s 撮合 · 上次 {{ live?.last_tick ?? '--' }}
      </span>
      <span class="text-[11px] text-content-faint">待执行 {{ live?.pending.length ?? 0 }} 条</span>
      <span class="text-[11px] text-content-faint">
        信号有效期 {{ live?.signal_ttl_minutes ?? 30 }} 分钟（开盘前信号顺延至 9:30）
      </span>
      <span v-if="live?.last_error" class="text-[11px] text-warn">{{ live.last_error }}</span>

      <div class="ml-auto flex items-center gap-2">
        <!-- 自动 / 手动开关 -->
        <div class="flex items-center gap-1 rounded-lg border border-base-600 bg-base-700/40 p-0.5">
          <button
            class="rounded-md px-3 py-1 text-[12px] font-medium transition-colors disabled:opacity-60"
            :class="live?.auto_trade ? 'bg-up/20 text-up' : 'text-content-muted hover:text-content'"
            :disabled="switching"
            @click="toggleMode(true)"
          >
            自动交易
          </button>
          <button
            class="rounded-md px-3 py-1 text-[12px] font-medium transition-colors disabled:opacity-60"
            :class="live && !live.auto_trade ? 'bg-base-600 text-content' : 'text-content-muted hover:text-content'"
            :disabled="switching"
            @click="toggleMode(false)"
          >
            手动交易
          </button>
        </div>
        <button
          v-if="live?.auto_trade"
          class="flex items-center gap-1.5 rounded-lg border border-base-600 px-3 py-1.5 text-[12px] text-content-muted transition-colors hover:border-primary/50 hover:text-content disabled:opacity-50"
          :disabled="ticking"
          @click="tickNow"
        >
          <Zap class="h-3.5 w-3.5" />
          立即撮合
        </button>
      </div>
    </div>

    <!-- 手动下单（手动模式） -->
    <div v-if="live && !live.auto_trade" class="glass-card p-5">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 class="text-subheading">手动下单</h3>
        <span class="text-[11px] text-content-faint">
          按实时价成交 · 整手 · T+1 · 自动撮合与止盈止损已暂停
        </span>
      </div>
      <div class="grid grid-cols-1 gap-3 md:grid-cols-12">
        <select
          v-model="order.code"
          class="rounded-lg border border-base-600 bg-base-700/40 px-3 py-2 text-[12px] text-content outline-none focus:border-primary/60 md:col-span-4"
        >
          <option value="">选择股票（自选 / 持仓）</option>
          <option v-for="o in tradeOptions" :key="o.code" :value="o.code">
            {{ o.name }} ({{ o.code }})
          </option>
        </select>

        <div class="flex items-center gap-1 rounded-lg border border-base-600 bg-base-700/40 p-0.5 md:col-span-2">
          <button
            class="flex-1 rounded-md px-2 py-1.5 text-[12px] font-medium transition-colors"
            :class="order.side === 'buy' ? 'bg-up/20 text-up' : 'text-content-muted hover:text-content'"
            @click="order.side = 'buy'"
          >
            买入
          </button>
          <button
            class="flex-1 rounded-md px-2 py-1.5 text-[12px] font-medium transition-colors"
            :class="order.side === 'sell' ? 'bg-down/20 text-down' : 'text-content-muted hover:text-content'"
            @click="order.side = 'sell'"
          >
            卖出
          </button>
        </div>

        <input
          v-if="order.side === 'buy'"
          v-model="order.amount"
          type="number"
          min="0"
          placeholder="买入金额（元）"
          class="rounded-lg border border-base-600 bg-base-700/40 px-3 py-2 text-[12px] text-content outline-none focus:border-primary/60 md:col-span-3"
        />
        <input
          v-else
          v-model="order.shares"
          type="number"
          min="0"
          step="100"
          placeholder="卖出股数（留空=全部）"
          class="rounded-lg border border-base-600 bg-base-700/40 px-3 py-2 text-[12px] text-content outline-none focus:border-primary/60 md:col-span-3"
        />

        <button
          class="rounded-lg px-4 py-2 text-[12px] font-medium text-white transition-opacity disabled:opacity-50 md:col-span-3"
          :class="order.side === 'buy' ? 'bg-up' : 'bg-down'"
          :disabled="ordering"
          @click="submitOrder"
        >
          {{ ordering ? '提交中...' : order.side === 'buy' ? '买入' : '卖出' }}
        </button>
      </div>

      <div class="mt-2 flex flex-wrap items-center gap-4 text-[11px]">
        <span v-if="orderPrice != null" class="font-num text-content-muted">
          现价 {{ orderPrice.toFixed(2) }}
        </span>
        <span v-if="order.side === 'buy' && orderPrice && Number(order.amount) > 0" class="font-num text-content-faint">
          约 {{ Math.floor(Number(order.amount) / orderPrice / 100) * 100 }} 股
        </span>
        <span v-if="orderMsg" class="font-medium" :class="orderOk ? 'text-up' : 'text-down'">
          {{ orderMsg }}
        </span>
      </div>
    </div>

    <!-- 待执行信号 -->
    <div v-if="live && live.pending.length > 0" class="glass-card p-4">
      <div class="mb-2 flex items-center justify-between">
        <h3 class="text-subheading">待执行信号</h3>
        <span class="text-[11px] text-content-faint">下个 tick 按实时价成交 · 同股以最新信号为准</span>
      </div>
      <div class="flex flex-wrap gap-2">
        <span
          v-for="p in live.pending"
          :key="p.code"
          class="inline-flex items-center gap-1.5 rounded-md border border-base-600 bg-base-700/40 px-2 py-1 text-[11px] text-content-muted"
          :title="p.reason"
        >
          <span :class="p.action === 'buy' ? 'text-up' : 'text-down'">
            {{ p.action === 'buy' ? '买' : '卖' }}
          </span>
          {{ p.name }}
          <span class="font-num text-content-faint">{{ p.code }}</span>
          <span class="font-num text-content-faint">· {{ p.waiting_seconds }}s</span>
        </span>
      </div>
    </div>

    <!-- 资金与净值 -->
    <div class="grid grid-cols-1 gap-5 xl:grid-cols-3">
      <div class="glass-card p-5">
        <h3 class="text-subheading mb-3">资金分布</h3>
        <div class="space-y-3">
          <div class="flex items-center justify-between">
            <span class="text-[12px] text-content-muted">可用现金</span>
            <span class="font-num text-[15px] font-semibold text-content">
              {{ account ? (account.cash / 10000).toFixed(2) + ' 万' : '--' }}
            </span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-[12px] text-content-muted">持仓市值</span>
            <span class="font-num text-[15px] font-semibold text-content">
              {{ account ? (account.market_value / 10000).toFixed(2) + ' 万' : '--' }}
            </span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-[12px] text-content-muted">浮动盈亏</span>
            <span class="font-num text-[15px] font-semibold" :class="pctTone(account?.total_pnl ?? 0)">
              {{ account ? (account.total_pnl > 0 ? '+' : '') + account.total_pnl.toFixed(0) + ' 元' : '--' }}
            </span>
          </div>
          <div class="flex items-center justify-between">
            <span class="text-[12px] text-content-muted">持仓只数</span>
            <span class="font-num text-[15px] font-semibold text-content">
              {{ account?.position_count ?? 0 }} / 10
            </span>
          </div>
        </div>
      </div>

      <div class="glass-card p-5 xl:col-span-2">
        <div class="mb-3 flex items-center justify-between">
          <h3 class="text-subheading">净值曲线</h3>
          <span class="text-[11px] text-content-faint">盘后 15:40 自动结算 · {{ equity.length }} 个交易日</span>
        </div>
        <v-chart v-if="equity.length > 0" :option="equityOption" style="height: 220px" autoresize />
        <div v-else class="flex h-[220px] flex-col items-center justify-center gap-2 text-content-faint">
          <p class="text-[13px]">暂无净值数据</p>
          <p class="text-[11px]">每个交易日收盘后自动记录（需后端保持运行）</p>
        </div>
      </div>
    </div>

    <!-- 当前持仓 -->
    <div class="glass-card p-5">
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-subheading">当前持仓</h3>
        <span class="text-[11px] text-content-faint">{{ account?.position_count ?? 0 }} 只</span>
      </div>
      <div v-if="account && account.positions.length > 0" class="overflow-x-auto">
        <table class="w-full text-left text-[12px]">
          <thead class="border-b border-base-600/60 text-content-faint">
            <tr>
              <th class="px-3 py-2 font-medium">代码</th>
              <th class="px-3 py-2 font-medium">名称</th>
              <th class="px-3 py-2 text-right font-medium">股数</th>
              <th class="px-3 py-2 text-right font-medium">成本价</th>
              <th class="px-3 py-2 text-right font-medium">现价</th>
              <th class="px-3 py-2 text-right font-medium">盈亏</th>
              <th class="px-3 py-2 text-right font-medium">盈亏%</th>
              <th class="px-3 py-2 font-medium">买入原因</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="p in account.positions"
              :key="p.code"
              class="cursor-pointer border-b border-base-600/30 transition-colors hover:bg-primary/10"
              @click="router.push(`/stock/${p.code}`)"
            >
              <td class="font-num px-3 py-2.5 text-content-muted">{{ p.code }}</td>
              <td class="px-3 py-2.5 font-medium text-content">{{ p.name }}</td>
              <td class="font-num px-3 py-2.5 text-right text-content">{{ p.shares }}</td>
              <td class="font-num px-3 py-2.5 text-right text-content-muted">{{ p.cost_price.toFixed(2) }}</td>
              <td class="font-num px-3 py-2.5 text-right text-content">{{ p.price.toFixed(2) }}</td>
              <td class="font-num px-3 py-2.5 text-right font-semibold" :class="pctTone(p.pnl)">
                {{ p.pnl > 0 ? '+' : '' }}{{ p.pnl.toFixed(0) }}
              </td>
              <td class="font-num px-3 py-2.5 text-right font-semibold" :class="pctTone(p.pnl_pct)">
                {{ p.pnl_pct > 0 ? '+' : '' }}{{ p.pnl_pct.toFixed(2) }}%
              </td>
              <td class="max-w-[260px] truncate px-3 py-2.5 text-content-faint" :title="p.strategy">
                {{ p.strategy }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="flex flex-col items-center gap-2 py-10 text-content-faint">
        <Wallet class="h-8 w-8 opacity-30" />
        <p class="text-[12px]">暂无持仓</p>
        <p class="text-[11px]">盘中实时撮合：建议进入队列后 5 秒内按实时价成交（需有自选股）</p>
      </div>
    </div>

    <!-- 交易流水 -->
    <div class="glass-card p-5">
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-subheading">交易流水</h3>
        <span class="text-[11px] text-content-faint">最近 {{ trades.length }} 笔</span>
      </div>
      <div v-if="trades.length > 0" class="max-h-[400px] overflow-y-auto">
        <table class="w-full text-left text-[12px]">
          <thead class="sticky top-0 bg-base-800 text-content-faint">
            <tr class="border-b border-base-600/60">
              <th class="px-3 py-2 font-medium">时间</th>
              <th class="px-3 py-2 font-medium">方向</th>
              <th class="px-3 py-2 font-medium">股票</th>
              <th class="px-3 py-2 text-right font-medium">成交价</th>
              <th class="px-3 py-2 text-right font-medium">股数</th>
              <th class="px-3 py-2 text-right font-medium">金额</th>
              <th class="px-3 py-2 text-right font-medium">费用</th>
              <th class="px-3 py-2 font-medium">原因</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="t in trades"
              :key="t.id"
              class="border-b border-base-600/30 transition-colors hover:bg-primary/10"
            >
              <td class="font-num px-3 py-2.5 text-content-muted">{{ t.trade_date }}</td>
              <td class="px-3 py-2.5">
                <span
                  class="inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium"
                  :class="t.side === 'buy' ? 'border-up/40 bg-up/10 text-up' : 'border-down/40 bg-down/10 text-down'"
                >
                  <ArrowUpCircle v-if="t.side === 'buy'" class="h-3 w-3" />
                  <ArrowDownCircle v-else class="h-3 w-3" />
                  {{ t.side === 'buy' ? '买入' : '卖出' }}
                </span>
              </td>
              <td class="px-3 py-2.5">
                <span class="text-content">{{ t.name }}</span>
                <span class="font-num ml-1.5 text-[11px] text-content-faint">{{ t.code }}</span>
              </td>
              <td class="font-num px-3 py-2.5 text-right text-content">{{ t.price.toFixed(2) }}</td>
              <td class="font-num px-3 py-2.5 text-right text-content">{{ t.shares }}</td>
              <td class="font-num px-3 py-2.5 text-right text-content">{{ (t.amount / 10000).toFixed(2) }}万</td>
              <td class="font-num px-3 py-2.5 text-right text-content-faint">{{ t.fee.toFixed(2) }}</td>
              <td class="max-w-[240px] truncate px-3 py-2.5 text-content-faint" :title="t.reason">
                {{ t.reason }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-else class="flex flex-col items-center gap-2 py-10 text-content-faint">
        <RefreshCw class="h-8 w-8 opacity-30" />
        <p class="text-[12px]">暂无交易记录</p>
        <p class="text-[11px]">自动撮合或手动下单成交后在此留下完整流水</p>
      </div>
    </div>
  </div>
</template>
