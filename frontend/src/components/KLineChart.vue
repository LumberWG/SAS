<script setup lang="ts">
/**
 * ECharts K线图：蜡烛图 + MA5/MA10/MA20 + 成交量副图。
 * A股配色：阳线红、阴线绿；色值统一取自 `lib/chartTheme`（与 Tailwind 色板同步）。
 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CandlestickChart, BarChart, LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
} from 'echarts/components'

import { axisLabel, chartNarrow, chartTheme, tooltipStyle } from '../lib/chartTheme'

use([
  CanvasRenderer,
  CandlestickChart,
  BarChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
])

const props = defineProps<{
  dates: string[]
  kline: number[][] // [open, close, low, high]
  volumes: number[]
  height?: string
  /** 上证指数日线（dates/closes），按日期对齐后画「相对强弱」副图；不传则不显示 */
  benchmark?: { dates: string[]; closes: (number | null)[] } | null
}>()

// MA 计算（与后端一致：简单算术平均）
function ma(values: number[], n: number): (number | null)[] {
  return values.map((_, i) => {
    if (i < n - 1) return null
    let sum = 0
    for (let j = i - n + 1; j <= i; j++) sum += values[j]
    return +(sum / n).toFixed(3)
  })
}

const closes = computed(() => props.kline.map((k) => k[1]))
const ma5 = computed(() => ma(closes.value, 5))
const ma10 = computed(() => ma(closes.value, 10))
const ma20 = computed(() => ma(closes.value, 20))

/** 基准按日期对齐（两边 date 可能带时间后缀，统一取前 10 位），只比较交易日 */
const benchmarkPct = computed(() => {
  const b = props.benchmark
  if (!b?.dates?.length) return null
  const byDate = new Map<string, number>()
  b.dates.forEach((d, i) => {
    const v = b.closes[i]
    if (v != null && v > 0) byDate.set(String(d).slice(0, 10), v)
  })
  const aligned = props.dates.map((d) => byDate.get(String(d).slice(0, 10)) ?? null)
  const base = aligned.find((v) => v != null)
  if (!base) return null
  return aligned.map((v) => (v == null ? null : +(((v / base) - 1) * 100).toFixed(2)))
})
const stockPct = computed(() => {
  const base = closes.value.find((v) => v > 0)
  if (!base) return []
  return closes.value.map((v) => +(((v / base) - 1) * 100).toFixed(2))
})
const hasBenchmark = computed(() => benchmarkPct.value !== null)

const option = computed(() => {
  // 每次重算都读当前主题：切主题 → 图表跟着换色（依赖在 chartTheme 内部登记）
  const c = chartTheme()
  const UP = c.up
  const DOWN = c.down
  const label = axisLabel(c)
  // 窄屏下 52+16 的固定边距会吃掉 1/4 绘图区；5 项 legend 也放不进一行
  const narrow = chartNarrow()
  const gridLeft = narrow ? 36 : 52
  const gridRight = narrow ? 8 : 16

  return {
  animation: false,
  backgroundColor: 'transparent',
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'cross', lineStyle: { color: c.textFaint } },
    ...tooltipStyle(c),
  },
  legend: {
    data: [
      'MA5', 'MA10', 'MA20',
      ...(hasBenchmark.value ? ['个股累计%', '上证累计%'] : []),
    ],
    type: narrow ? 'scroll' : 'plain',
    textStyle: { color: c.textMuted, fontSize: 12 },
    top: 0,
    icon: 'roundRect',
    itemWidth: 12,
    itemHeight: 3,
  },
  grid: [
    { left: gridLeft, right: gridRight, top: 28, height: hasBenchmark.value ? '46%' : '58%' },
    { left: gridLeft, right: gridRight, top: hasBenchmark.value ? '60%' : '74%', height: '14%' },
    ...(hasBenchmark.value ? [{ left: gridLeft, right: gridRight, top: '80%', height: '14%' }] : []),
  ],
  xAxis: [
    {
      type: 'category',
      data: props.dates,
      axisLine: { lineStyle: { color: c.axisLine } },
      axisLabel: label,
      boundaryGap: true,
    },
    {
      type: 'category',
      gridIndex: 1,
      data: props.dates,
      axisLabel: { show: false },
      axisLine: { lineStyle: { color: c.axisLine } },
      boundaryGap: true,
    },
    ...(hasBenchmark.value ? [{
      type: 'category',
      gridIndex: 2,
      data: props.dates,
      axisLabel: { show: false },
      axisLine: { lineStyle: { color: c.axisLine } },
      boundaryGap: true,
    }] : []),
  ],
  yAxis: [
    {
      scale: true,
      axisLabel: label,
      splitLine: { lineStyle: { color: c.splitLine, type: 'dashed' } },
    },
    {
      scale: true,
      gridIndex: 1,
      axisLabel: label,
      splitLine: { show: false },
    },
    ...(hasBenchmark.value ? [{
      scale: true,
      gridIndex: 2,
      axisLabel: { color: c.textFaint, fontSize: 11, formatter: '{value}%' },
      splitLine: { lineStyle: { color: c.splitLine, type: 'dashed' } },
    }] : []),
  ],
  dataZoom: [
    { type: 'inside', xAxisIndex: hasBenchmark.value ? [0, 1, 2] : [0, 1], start: 60, end: 100 },
  ],
  series: [
    {
      name: 'K线',
      type: 'candlestick',
      data: props.kline,
      itemStyle: {
        color: UP, // 阳线填充（红）
        color0: DOWN, // 阴线填充（绿）
        borderColor: UP,
        borderColor0: DOWN,
      },
    },
    {
      name: 'MA5',
      type: 'line',
      data: ma5.value,
      smooth: true,
      lineStyle: { width: 1, color: c.primaryLight },
      symbol: 'none',
    },
    {
      name: 'MA10',
      type: 'line',
      data: ma10.value,
      smooth: true,
      lineStyle: { width: 1, color: c.warn },
      symbol: 'none',
    },
    {
      name: 'MA20',
      type: 'line',
      data: ma20.value,
      smooth: true,
      lineStyle: { width: 1, color: c.accent },
      symbol: 'none',
    },
    {
      name: '成交量',
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: props.volumes.map((v, i) => ({
        value: v,
        itemStyle: {
          color: props.kline[i][1] >= props.kline[i][0] ? UP : DOWN,
        },
      })),
    },
    // 相对强弱：两条线都归一到「窗口首日 = 0%」，避免把指数点位和个股价格放在同一轴上误读
    ...(hasBenchmark.value ? [
      {
        name: '个股累计%',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: stockPct.value,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.5, color: c.primaryLight },
      },
      {
        name: '上证累计%',
        type: 'line',
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: benchmarkPct.value,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1.5, color: c.accent, type: 'dashed' },
      },
    ] : []),
  ],
  }
})
</script>

<template>
  <v-chart class="chart" :option="option" :style="{ height: chartNarrow() ? '320px' : (height ?? '420px') }" autoresize />
</template>

<style scoped>
.chart {
  width: 100%;
}
</style>
