<script setup lang="ts">
/**
 * 模拟交易：10万模拟盘自动执行系统买卖建议，验证选股思路准确性。
 * 数据：账户/持仓/流水/净值（30s 轮询）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowDownCircle,
  ArrowUpCircle,
  RefreshCw,
  RotateCcw,
  Wallet,
} from 'lucide-vue-next'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'

import {
  getPaperAccount,
  getPaperEquity,
  getPaperTrades,
  resetPaperAccount,
  type PaperAccount,
  type PaperTrade,
} from '../api'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])

const router = useRouter()

const account = ref<PaperAccount | null>(null)
const trades = ref<PaperTrade[]>([])
const equity = ref<Array<{ date: string; total_asset: number }>>([])
const resetting = ref(false)
let timer: number | undefined

async function load() {
  try {
    const [acc, tradeData, equityData] = await Promise.all([
      getPaperAccount(),
      getPaperTrades(100),
      getPaperEquity(),
    ])
    account.value = acc
    trades.value = tradeData.trades
    equity.value = equityData.equity.map((e) => ({
      date: e.date.slice(5),
      total_asset: e.total_asset,
    }))
  } catch {
    // 保留旧数据
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

onMounted(() => {
  load()
  timer = window.setInterval(load, 30000)
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
            系统建议自动执行 · 等分仓位最多10只 · 整手 · T+1 · 佣金万2.5+印花税千1
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
        <p class="text-[11px]">开盘 9:20 / 盘中每 5 分钟自动执行买入建议（需有自选股）</p>
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
        <p class="text-[11px]">系统自动执行建议后在此留下完整流水</p>
      </div>
    </div>
  </div>
</template>
