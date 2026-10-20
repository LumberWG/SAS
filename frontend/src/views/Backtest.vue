<script setup lang="ts">
/**
 * 回测中心：参数表单 → 提交回测 → 净值曲线 + 绩效指标 + 交易明细。
 */
import { onMounted, ref } from 'vue'
import { Play } from 'lucide-vue-next'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'

import {
  getStrategies,
  runBacktest,
  type BacktestResult,
  type StrategyInfo,
} from '../api'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])

const strategies = ref<StrategyInfo[]>([])
const form = ref({
  strategy: '',
  start_date: '2025-01-01',
  end_date: '2026-10-20',
  initial_capital: 1000000,
  max_positions: 5,
  universe_size: 300,
})
const result = ref<BacktestResult | null>(null)
const loading = ref(false)
const error = ref('')

onMounted(async () => {
  strategies.value = await getStrategies()
  if (strategies.value.length > 0) {
    form.value.strategy = strategies.value[0].name
  }
})

async function submit() {
  loading.value = true
  error.value = ''
  try {
    result.value = await runBacktest(form.value)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '回测请求失败'
  } finally {
    loading.value = false
  }
}

const equityOption = ref({})
function buildEquityOption(data: BacktestResult) {
  const dates = data.equity_curve.map((p) => p.date)
  const equity = data.equity_curve.map((p) => p.equity)
  equityOption.value = {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 70, right: 20, top: 20, bottom: 40 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#111A2E',
      borderColor: '#243354',
      textStyle: { color: '#E6EDF7', fontSize: 12 },
      valueFormatter: (v: number) => v.toLocaleString('zh-CN', { maximumFractionDigits: 0 }) + ' 元',
    },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: '#5C6B85', fontSize: 10 },
      axisLine: { lineStyle: { color: '#243354' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: '#5C6B85', fontSize: 10, formatter: (v: number) => (v / 10000).toFixed(0) + '万' },
      splitLine: { lineStyle: { color: 'rgba(36,51,84,0.6)' } },
    },
    series: [
      {
        type: 'line',
        data: equity,
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
      },
    ],
  }
}

function metricTone(v: number): string {
  if (v > 0) return 'text-up'
  if (v < 0) return 'text-down'
  return 'text-content'
}
</script>

<template>
  <div class="space-y-5">
    <!-- 参数表单 -->
    <div class="glass-card p-5">
      <h3 class="mb-4 text-subheading">回测参数</h3>
      <div class="grid grid-cols-1 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">策略</span>
          <select v-model="form.strategy" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none focus:border-primary">
            <option v-for="s in strategies" :key="s.name" :value="s.name">{{ s.name }}</option>
          </select>
        </label>
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">开始日期</span>
          <input v-model="form.start_date" type="date" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none focus:border-primary" />
        </label>
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">结束日期</span>
          <input v-model="form.end_date" type="date" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none focus:border-primary" />
        </label>
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">初始资金（元）</span>
          <input v-model.number="form.initial_capital" type="number" min="10000" step="100000" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 font-num text-[13px] text-content outline-none focus:border-primary" />
        </label>
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">最大持仓数</span>
          <input v-model.number="form.max_positions" type="number" min="1" max="20" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 font-num text-[13px] text-content outline-none focus:border-primary" />
        </label>
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">股票池（成交额前N）</span>
          <input v-model.number="form.universe_size" type="number" min="10" max="3000" step="50" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 font-num text-[13px] text-content outline-none focus:border-primary" />
        </label>
      </div>
      <div class="mt-4 flex items-center gap-4">
        <button
          class="flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-primary-light px-5 py-2.5 text-[13px] font-semibold text-white shadow-glow transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="loading"
          @click="submit"
        >
          <Play class="h-4 w-4" />
          {{ loading ? '回测运行中...' : '开始回测' }}
        </button>
        <span v-if="error" class="text-[12px] text-up">{{ error }}</span>
        <span class="text-[11px] text-content-faint">规则：信号日收盘买入，止损/止盈/到期卖出，佣金万2.5+印花税千0.5</span>
      </div>
    </div>

    <!-- 结果 -->
    <template v-if="result">
      <!-- 绩效指标卡 -->
      <div class="grid grid-cols-2 gap-4 md:grid-cols-4">
        <div class="glass-card p-4">
          <div class="text-[12px] text-content-muted">总收益率</div>
          <div class="font-num mt-1 text-[24px] font-semibold" :class="metricTone(result.metrics.total_return)">
            {{ result.metrics.total_return > 0 ? '+' : '' }}{{ result.metrics.total_return.toFixed(2) }}%
          </div>
          <div class="mt-1 text-[11px] text-content-faint">年化 {{ result.metrics.annualized_return.toFixed(2) }}%</div>
        </div>
        <div class="glass-card p-4">
          <div class="text-[12px] text-content-muted">最大回撤</div>
          <div class="font-num mt-1 text-[24px] font-semibold text-down">
            {{ result.metrics.max_drawdown.toFixed(2) }}%
          </div>
          <div class="mt-1 text-[11px] text-content-faint">期末净值 {{ result.metrics.final_equity.toLocaleString('zh-CN') }}</div>
        </div>
        <div class="glass-card p-4">
          <div class="text-[12px] text-content-muted">夏普比率</div>
          <div class="font-num mt-1 text-[24px] font-semibold text-content">
            {{ result.metrics.sharpe.toFixed(2) }}
          </div>
          <div class="mt-1 text-[11px] text-content-faint">年化，无风险利率 0</div>
        </div>
        <div class="glass-card p-4">
          <div class="text-[12px] text-content-muted">胜率</div>
          <div class="font-num mt-1 text-[24px] font-semibold text-content">
            {{ result.metrics.win_rate.toFixed(1) }}%
          </div>
          <div class="mt-1 text-[11px] text-content-faint">{{ result.metrics.trade_count }} 笔交易 / {{ result.metrics.trading_days }} 交易日</div>
        </div>
      </div>

      <!-- 净值曲线 -->
      <div class="glass-card p-5">
        <h3 class="mb-3 text-subheading">净值曲线</h3>
        <v-chart :option="equityOption" style="height: 320px" autoresize />
      </div>

      <!-- 交易明细 -->
      <div class="glass-card overflow-auto">
        <div class="border-b border-base-600/60 px-5 py-3">
          <h3 class="text-subheading">交易明细</h3>
        </div>
        <table class="w-full text-left text-[13px]">
          <thead class="text-[11px] text-content-faint">
            <tr class="border-b border-base-600/60">
              <th class="px-5 py-2.5 font-medium">代码</th>
              <th class="px-5 py-2.5 font-medium">名称</th>
              <th class="px-5 py-2.5 font-medium">买入日</th>
              <th class="px-5 py-2.5 text-right font-medium">买入价</th>
              <th class="px-5 py-2.5 font-medium">卖出日</th>
              <th class="px-5 py-2.5 text-right font-medium">卖出价</th>
              <th class="px-5 py-2.5 text-right font-medium">盈亏</th>
              <th class="px-5 py-2.5 font-medium">原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(t, i) in result.trades" :key="i" class="border-b border-base-600/30">
              <td class="font-num px-5 py-2.5 text-content-muted">{{ t.code }}</td>
              <td class="px-5 py-2.5 text-content">{{ t.name }}</td>
              <td class="font-num px-5 py-2.5 text-content-muted">{{ t.buy_date }}</td>
              <td class="font-num px-5 py-2.5 text-right text-content">{{ t.buy_price.toFixed(2) }}</td>
              <td class="font-num px-5 py-2.5 text-content-muted">{{ t.sell_date }}</td>
              <td class="font-num px-5 py-2.5 text-right text-content">{{ t.sell_price.toFixed(2) }}</td>
              <td class="font-num px-5 py-2.5 text-right font-semibold" :class="t.pnl >= 0 ? 'text-up' : 'text-down'">
                {{ t.pnl_pct > 0 ? '+' : '' }}{{ t.pnl_pct.toFixed(2) }}%
              </td>
              <td class="px-5 py-2.5">
                <span class="rounded-md border border-base-600 px-2 py-0.5 text-[11px] text-content-muted">{{ t.reason }}</span>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="result.trades.length === 0" class="p-8 text-center text-[13px] text-content-faint">
          回测区间内无交易
        </div>
      </div>
    </template>

    <div v-else-if="!loading" class="glass-card flex h-64 flex-col items-center justify-center gap-2 text-content-faint">
      <p class="text-[13px]">设置参数后点击「开始回测」</p>
      <p class="text-[11px]">缺省股票池为实时成交额前 N 只，K线优先使用本地缓存</p>
    </div>
  </div>
</template>
