<script setup lang="ts">
/**
 * 回测中心：参数表单 → 提交回测 → 净值曲线 + 绩效指标 + 交易明细。
 */
import { ref, reactive } from 'vue'
import { Play, ChevronDown } from 'lucide-vue-next'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'

import { runBacktest, type BacktestResult } from '../api'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])

const form = reactive<Record<string, any>>({
  start_date: '2025-01-01',
  end_date: '2026-10-20',
  initial_capital: null,
  max_positions: null,
  universe_size: 500,
  board_prefixes: [],
  watch_cap: null,
  add_per_day: null,
  rebalance_days: null,
  buy_threshold: null,
  buy_top_pct: null,
  max_open_gain: null,
  max_gain_5d: null,
  min_volume_ratio: null,
  min_turnover_rate: null,
  queue_top_m: null,
  entry_max_chase: null,
  entry_pullback: null,
  invert_score: null,
  stop_loss: null,
  take_profit: null,
  max_hold_days: null,
  stop_atr_mult: null,
  stop_atr_min: null,
  stop_atr_max: null,
  stop_confirm_close: null,
  partial_tp_at: null,
  partial_tp_ratio: null,
  breakeven_trigger: null,
  trail_pct: null,
  trail_arm: null,
  regime_ma: null,
  regime_fast_ma: null,
  weak_exposure: null,
  regime_flat_below: null,
  max_total_exposure: null,
  single_position_ratio: null,
  full_score: null,
  half_score: null,
  rotation_enabled: null,
  rotation_gap: null,
  rotation_min_score: null,
})
const formOpen = ref(true)
const result = ref<BacktestResult | null>(null)
const loading = ref(false)
const error = ref('')

interface ParamDef {
  key: string
  label: string
  type: 'number' | 'bool' | 'multiselect'
  min?: number
  max?: number
  step?: number
  unit?: string
  options?: [string, string][]
}
const groups: { name: string; items: ParamDef[] }[] = [
  { name: '基础', items: [
    { key: 'initial_capital', label: '初始资金', type: 'number', step: 10000 },
    { key: 'max_positions', label: '最大持仓数', type: 'number', min: 1, max: 50 },
    { key: 'universe_size', label: '股票池(TopN)', type: 'number', min: 10, max: 6000 },
    { key: 'board_prefixes', label: '板块', type: 'multiselect', options: [['600', '沪主'], ['000', '深主'], ['300', '创业板'], ['688', '科创板'], ['920', '北交所']] },
    { key: 'watch_cap', label: '自选上限', type: 'number', min: 10, max: 500 },
    { key: 'add_per_day', label: '每日加自选', type: 'number', min: 1, max: 200 },
  ]},
  { name: '选股/买入', items: [
    { key: 'rebalance_days', label: '调仓周期(天)', type: 'number', min: 1, max: 20 },
    { key: 'buy_threshold', label: '14因子买入阈值', type: 'number' },
    { key: 'buy_top_pct', label: '买入分位门槛', type: 'number', min: 0, max: 1, step: 0.05, unit: '0~1' },
    { key: 'max_open_gain', label: '次日开盘涨幅上限', type: 'number', unit: '不追高' },
    { key: 'max_gain_5d', label: '近5日涨幅上限', type: 'number', unit: '不追已涨' },
    { key: 'min_volume_ratio', label: '量比下限', type: 'number' },
    { key: 'min_turnover_rate', label: '换手率下限', type: 'number', unit: '%' },
    { key: 'invert_score', label: '反转选股', type: 'bool' },
  ]},
  { name: '五段式(队列/切入点)', items: [
    { key: 'queue_top_m', label: '交易队列topM', type: 'number', min: 0, max: 50, unit: '每日买单数' },
    { key: 'entry_max_chase', label: '不追高', type: 'number', step: 0.005, unit: '开盘溢价上限' },
    { key: 'entry_pullback', label: '回踩要求', type: 'number', step: 0.005, unit: '0=不限' },
  ]},
  { name: '止损/止盈', items: [
    { key: 'stop_loss', label: '止损比例', type: 'number', min: 0.01, max: 0.5, step: 0.01, unit: '成本×' },
    { key: 'take_profit', label: '止盈比例', type: 'number', step: 0.05, unit: '成本×' },
    { key: 'max_hold_days', label: '时间止损(天)', type: 'number', min: 0, max: 250 },
    { key: 'stop_atr_mult', label: 'ATR止损倍数', type: 'number', step: 0.5 },
    { key: 'stop_atr_min', label: 'ATR距离下限', type: 'number', step: 0.01 },
    { key: 'stop_atr_max', label: 'ATR距离上限', type: 'number', step: 0.01 },
    { key: 'stop_confirm_close', label: '收盘确认止损', type: 'bool' },
    { key: 'partial_tp_at', label: '分段止盈触发', type: 'number', step: 0.05 },
    { key: 'partial_tp_ratio', label: '分段止盈比例', type: 'number', min: 0, max: 1, step: 0.05 },
  ]},
  { name: '移动/保本止损', items: [
    { key: 'breakeven_trigger', label: '保本触发浮盈', type: 'number', step: 0.05 },
    { key: 'trail_pct', label: '移动回撤', type: 'number', step: 0.05 },
    { key: 'trail_arm', label: '移动启用门槛', type: 'number', step: 0.05 },
  ]},
  { name: '大盘择时', items: [
    { key: 'regime_ma', label: '择时均线', type: 'number', min: 2, max: 250 },
    { key: 'regime_fast_ma', label: '快线周期', type: 'number', min: 2, max: 250 },
    { key: 'weak_exposure', label: '弱市仓位上限', type: 'number', min: 0, max: 1, step: 0.05 },
    { key: 'regime_flat_below', label: '最弱档清仓', type: 'bool' },
  ]},
  { name: '仓位/评分/轮动', items: [
    { key: 'max_total_exposure', label: '总仓位上限', type: 'number', min: 0, max: 1, step: 0.05 },
    { key: 'single_position_ratio', label: '单票上限', type: 'number', min: 0, max: 1, step: 0.05 },
    { key: 'full_score', label: '全额评分档', type: 'number' },
    { key: 'half_score', label: '半额评分档', type: 'number' },
    { key: 'rotation_enabled', label: '满仓轮动', type: 'bool' },
    { key: 'rotation_gap', label: '轮动分差', type: 'number' },
    { key: 'rotation_min_score', label: '轮动最低分', type: 'number' },
  ]},
  { name: '做T引擎', items: [
    { key: 't_active', label: '启用做T', type: 'bool' },
    { key: 't_size', label: '做T比例', type: 'number', min: 0, max: 1, step: 0.05 },
    { key: 't_up_sell', label: '涨超卖T', type: 'number', min: 0, max: 0.2, step: 0.01 },
    { key: 't_up_buyback', label: '回调买回', type: 'number', min: 0, max: 0.2, step: 0.01 },
    { key: 't_down_add', label: '跌加仓', type: 'number', min: 0, max: 0.2, step: 0.01 },
    { key: 't_down_full', label: '跌满仓', type: 'number', min: 0, max: 0.2, step: 0.01 },
    { key: 't_rebound_sell', label: '反弹减仓', type: 'number', min: 0, max: 0.2, step: 0.01 },
    { key: 't_limit_sell_half', label: '涨停卖半', type: 'bool' },
    { key: 't_crash_guard', label: '大盘护栏', type: 'number', min: 0, max: 0.1, step: 0.01 },
  ]},
]

function toggleBoard(val: string) {
  const arr = form.board_prefixes as string[]
  const i = arr.indexOf(val)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(val)
}

function buildPayload(): Record<string, unknown> {
  const p: Record<string, unknown> = {
    start_date: form.start_date,
    end_date: form.end_date,
  }
  for (const g of groups) {
    for (const it of g.items) {
      const v = form[it.key]
      if (v !== null && v !== undefined && v !== '') p[it.key] = v
    }
  }
  return p
}

async function submit() {
  loading.value = true
  error.value = ''
  try {
    result.value = await runBacktest(buildPayload() as any)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '回测请求失败'
  } finally {
    loading.value = false
  }
}

// 回测预设：一键把心得/线上参数载入表单（未列出的项留空=沿用线上默认）
const presets: { name: string; params: Record<string, unknown> }[] = [
  {
    name: '创业板超卖半仓(心得)',
    params: {
      board_prefixes: ['300'],
      max_positions: 4,
      universe_size: 600,
      rebalance_days: 1,
      buy_top_pct: 0.3,
      max_open_gain: 0.03,
      max_gain_5d: 0.08,
      min_volume_ratio: 1.0,
      max_total_exposure: 0.5,
      single_position_ratio: 0.25,
      stop_loss: 0.08,
      take_profit: 0.20,
      stop_atr_mult: 2.0,
      stop_atr_min: 0.04,
      stop_atr_max: 0.12,
      breakeven_trigger: 0.10,
      trail_pct: 0.10,
      regime_ma: 60,
      regime_flat_below: true,
    },
  },
  {
    name: '创业板超卖·狠版(微调)',
    params: {
      board_prefixes: ['300'],
      max_positions: 4,
      universe_size: 600,
      rebalance_days: 1,
      buy_top_pct: 0.2,
      max_open_gain: 0.03,
      max_gain_5d: 0.05,
      min_volume_ratio: 1.0,
      max_total_exposure: 0.5,
      single_position_ratio: 0.25,
      stop_loss: 0.08,
      take_profit: 0.20,
      stop_atr_mult: 2.0,
      stop_atr_min: 0.04,
      stop_atr_max: 0.12,
      breakeven_trigger: 0.10,
      trail_pct: 0.10,
      partial_tp_at: 0.15,
      partial_tp_ratio: 0.5,
      regime_ma: 60,
      regime_flat_below: true,
    },
  },
  {
    name: '弱市防守版(收盘确认+做T不加仓)',
    params: {
      board_prefixes: ['300'],
      max_positions: 4,
      universe_size: 600,
      rebalance_days: 1,
      buy_top_pct: 0.3,
      max_open_gain: 0.03,
      max_gain_5d: 0.08,
      min_volume_ratio: 1.0,
      max_total_exposure: 0.5,
      single_position_ratio: 0.25,
      stop_loss: 0.08,
      take_profit: 0.20,
      stop_atr_mult: 2.0,
      stop_atr_min: 0.04,
      stop_atr_max: 0.12,
      stop_confirm_close: true,
      breakeven_trigger: 0.10,
      trail_pct: 0.10,
      regime_ma: 120,
      regime_flat_below: true,
      t_active: true,
      t_size: 0.25,
      t_up_sell: 0.05,
      t_up_buyback: 0.03,
      t_down_add: 0.20,
      t_down_full: 0.20,
      t_rebound_sell: 0.02,
      t_limit_sell_half: true,
      t_crash_guard: 0.02,
    },
  },
]

function applyPreset(p: { name: string; params: Record<string, unknown> }) {
  for (const k in p.params) {
    form[k] = p.params[k]
  }
}

const equityOption = ref({})
function buildEquityOption(data: BacktestResult) {
  const dates = data.equity_curve.map((p) => p.date)
  const equity = data.equity_curve.map((p) => p.equity)
  const benchmark = data.equity_curve.map((p) => p.benchmark ?? null)
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
        name: '策略净值',
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
      {
        name: '等权基准',
        type: 'line',
        data: benchmark,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.5, color: '#94A3B8', type: 'dashed' },
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
    <!-- 参数表单（可展开/收起） -->
    <div class="glass-card p-5">
      <div class="flex items-center justify-between">
        <h3 class="text-subheading">回测参数</h3>
        <button
          type="button"
          class="flex items-center gap-1 rounded-md border border-base-600 bg-base-700 px-2.5 py-1 text-[12px] text-content-muted transition-colors hover:border-primary/60 hover:text-primary"
          @click="formOpen = !formOpen"
        >
          <ChevronDown class="h-4 w-4 transition-transform" :class="formOpen ? 'rotate-180' : ''" />
          {{ formOpen ? '收起' : '展开' }}
        </button>
      </div>
      <div v-show="formOpen" class="mt-4">
      <div class="mb-4 flex flex-wrap items-center gap-2">
        <span class="text-[12px] text-content-muted">快速预设：</span>
        <button
          v-for="p in presets"
          :key="p.name"
          type="button"
          @click="applyPreset(p)"
          class="rounded-md border border-base-600 bg-base-700 px-3 py-1.5 text-[12px] text-content transition-colors hover:border-primary/60 hover:text-primary"
        >
          {{ p.name }}
        </button>
        <span class="text-[11px] text-content-faint">预设只填常用项，其余留空=沿用线上默认</span>
      </div>
      <div class="grid grid-cols-1 gap-4 md:grid-cols-3">
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">开始日期</span>
          <input v-model="form.start_date" type="date" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none focus:border-primary" />
        </label>
        <label class="flex flex-col gap-1.5">
          <span class="text-[11px] text-content-muted">结束日期</span>
          <input v-model="form.end_date" type="date" class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none focus:border-primary" />
        </label>
        <div class="flex flex-col justify-center gap-1 rounded-lg border border-base-600/60 bg-base-700/40 px-3 py-2">
          <span class="text-[11px] text-content-muted">自动执行（无需选策略）</span>
          <span class="text-[12px] text-content">全策略扫描 → 加自选 → 14因子排序 → 评分加权下单</span>
        </div>
      </div>

      <!-- 可调参数分组 -->
      <div class="mt-5 space-y-4">
        <div v-for="g in groups" :key="g.name" class="rounded-lg border border-base-600/50 bg-base-800/30 p-3">
          <div class="mb-2 text-[12px] font-medium text-content-muted">{{ g.name }}</div>
          <div class="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-4">
            <template v-for="it in g.items" :key="it.key">
              <label v-if="it.type === 'number'" class="flex flex-col gap-1">
                <span class="text-[11px] text-content-muted">{{ it.label }}<span v-if="it.unit" class="text-content-faint"> · {{ it.unit }}</span></span>
                <input v-model.number="form[it.key]" type="number" :min="it.min" :max="it.max" :step="it.step" placeholder="默认" class="rounded-lg border border-base-600 bg-base-700 px-2.5 py-1.5 text-[13px] text-content outline-none focus:border-primary" />
              </label>
              <label v-else-if="it.type === 'bool'" class="flex items-center justify-between rounded-lg border border-base-600 bg-base-700 px-3 py-2">
                <span class="text-[11px] text-content-muted">{{ it.label }}</span>
                <input v-model="form[it.key]" type="checkbox" class="h-4 w-4 accent-primary" />
              </label>
              <div v-else-if="it.type === 'multiselect'" class="col-span-2 flex flex-col gap-1 md:col-span-1">
                <span class="text-[11px] text-content-muted">{{ it.label }}</span>
                <div class="flex flex-wrap gap-1.5">
                  <button v-for="[val, lab] in it.options" :key="val" type="button" @click="toggleBoard(val)"
                    :class="form[it.key].includes(val) ? 'border-primary/60 bg-primary/20 text-primary' : 'border-base-600 bg-base-700 text-content-muted'"
                    class="rounded-md border px-2 py-1 text-[11px] transition-colors">{{ lab }}</button>
                </div>
              </div>
            </template>
          </div>
        </div>
      </div>
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
        <span v-if="loading" class="text-[12px] text-content-muted">回测在后台运行，约需 1~3 分钟，请勿关闭页面</span>
        <span v-if="error" class="text-[12px] text-up">{{ error }}</span>
        <span class="text-[11px] text-content-faint">留空=沿用线上默认；板块不选=全市场</span>
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
          <div class="mt-1 text-[11px] text-content-faint">
            年化 {{ result.metrics.annualized_return.toFixed(2) }}%
          </div>
          <div
            v-if="result.metrics.benchmark_return !== undefined"
            class="mt-0.5 text-[11px]"
            :class="(result.metrics.total_return - result.metrics.benchmark_return) >= 0 ? 'text-up' : 'text-down'"
          >
            跑赢基准 {{ (result.metrics.total_return - result.metrics.benchmark_return) >= 0 ? '+' : '' }}{{ (result.metrics.total_return - result.metrics.benchmark_return).toFixed(2) }}%
            <span class="text-content-faint">（基准 {{ result.metrics.benchmark_return.toFixed(2) }}%）</span>
          </div>
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

      <!-- 亏损归因与调参建议 -->
      <div v-if="result.review" class="glass-card p-5">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <h3 class="text-subheading">亏损归因</h3>
          <span class="text-[11px] text-content-faint">
            平均持有 {{ result.review.avg_hold_days }} 个交易日 · 费用 {{ result.review.cost.fee_total.toFixed(0) }} 元
            （占亏损 {{ result.review.cost.fee_vs_loss_pct }}%）
          </span>
        </div>
        <p class="mt-2 text-[12px] text-content-muted">{{ result.review.summary }}</p>

        <div class="mt-4 grid grid-cols-1 gap-5 lg:grid-cols-2">
          <div>
            <div class="mb-2 text-[12px] text-content-muted">按卖出原因</div>
            <table class="w-full text-left text-[12px]">
              <thead class="text-[11px] text-content-faint">
                <tr class="border-b border-base-600/60">
                  <th class="py-1.5 font-medium">原因</th>
                  <th class="py-1.5 text-right font-medium">笔数</th>
                  <th class="py-1.5 text-right font-medium">平均</th>
                  <th class="py-1.5 text-right font-medium">胜率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="g in result.review.reasons" :key="g.name" class="border-b border-base-600/30">
                  <td class="py-1.5 text-content">{{ g.name }}</td>
                  <td class="font-num py-1.5 text-right text-content-muted">{{ g.count }}</td>
                  <td class="font-num py-1.5 text-right" :class="metricTone(g.avg_pnl_pct)">
                    {{ g.avg_pnl_pct > 0 ? '+' : '' }}{{ g.avg_pnl_pct.toFixed(2) }}%
                  </td>
                  <td class="font-num py-1.5 text-right text-content-muted">{{ g.win_rate.toFixed(0) }}%</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div>
            <div class="mb-2 text-[12px] text-content-muted">按持有天数</div>
            <table class="w-full text-left text-[12px]">
              <thead class="text-[11px] text-content-faint">
                <tr class="border-b border-base-600/60">
                  <th class="py-1.5 font-medium">持有</th>
                  <th class="py-1.5 text-right font-medium">笔数</th>
                  <th class="py-1.5 text-right font-medium">平均</th>
                  <th class="py-1.5 text-right font-medium">胜率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="g in result.review.hold_buckets" :key="g.name" class="border-b border-base-600/30">
                  <td class="py-1.5 text-content">{{ g.name }}</td>
                  <td class="font-num py-1.5 text-right text-content-muted">{{ g.count }}</td>
                  <td class="font-num py-1.5 text-right" :class="metricTone(g.avg_pnl_pct)">
                    {{ g.avg_pnl_pct > 0 ? '+' : '' }}{{ g.avg_pnl_pct.toFixed(2) }}%
                  </td>
                  <td class="font-num py-1.5 text-right text-content-muted">{{ g.win_rate.toFixed(0) }}%</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="mt-5">
          <div class="mb-2 text-[12px] text-content-muted">调参建议（按优先级）</div>
          <ul class="space-y-2">
            <li
              v-for="(s, i) in result.review.suggestions"
              :key="i"
              class="rounded-lg border border-base-600/60 bg-base-700/40 px-3 py-2"
            >
              <div class="flex flex-wrap items-center gap-2">
                <span
                  class="rounded-md px-1.5 py-0.5 text-[10px]"
                  :class="s.priority === '高' ? 'bg-down/15 text-down' : s.priority === '中' ? 'bg-base-600/50 text-content' : 'bg-base-600/30 text-content-muted'"
                >{{ s.priority }}</span>
                <span class="text-[12px] font-medium text-content">{{ s.target }}</span>
                <span class="font-num text-[11px] text-content-muted">{{ s.current }} → {{ s.suggest }}</span>
              </div>
              <p class="mt-1 text-[11px] leading-relaxed text-content-faint">{{ s.reason }}</p>
            </li>
          </ul>
        </div>
      </div>

      <!-- 净值曲线（策略 vs 等权基准） -->
      <div class="glass-card p-5">
        <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h3 class="text-subheading">净值曲线</h3>
          <div class="flex items-center gap-4 text-[11px] text-content-muted">
            <span class="flex items-center gap-1.5">
              <span class="inline-block h-2 w-4 rounded-full bg-[#38BDF8]"></span>策略净值
            </span>
            <span class="flex items-center gap-1.5">
              <span class="inline-block h-2 w-4 rounded-full bg-[#94A3B8]"></span>等权基准
            </span>
          </div>
        </div>
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
