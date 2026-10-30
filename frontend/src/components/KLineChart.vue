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

import { axisLabel, chart, splitLine, tooltipStyle } from '../lib/chartTheme'

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
}>()

const UP = chart.up
const DOWN = chart.down

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

const option = computed(() => ({
  animation: false,
  backgroundColor: 'transparent',
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'cross', lineStyle: { color: chart.textFaint } },
    ...tooltipStyle,
  },
  legend: {
    data: ['MA5', 'MA10', 'MA20'],
    textStyle: { color: chart.textMuted, fontSize: 11 },
    top: 0,
    icon: 'roundRect',
    itemWidth: 12,
    itemHeight: 3,
  },
  grid: [
    { left: 52, right: 16, top: 28, height: '58%' },
    { left: 52, right: 16, top: '74%', height: '16%' },
  ],
  xAxis: [
    {
      type: 'category',
      data: props.dates,
      axisLine: { lineStyle: { color: chart.axisLine } },
      axisLabel,
      boundaryGap: true,
    },
    {
      type: 'category',
      gridIndex: 1,
      data: props.dates,
      axisLabel: { show: false },
      axisLine: { lineStyle: { color: chart.axisLine } },
      boundaryGap: true,
    },
  ],
  yAxis: [
    {
      scale: true,
      axisLabel,
      splitLine,
    },
    {
      scale: true,
      gridIndex: 1,
      axisLabel,
      splitLine: { show: false },
    },
  ],
  dataZoom: [
    { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
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
      lineStyle: { width: 1, color: chart.primaryLight },
      symbol: 'none',
    },
    {
      name: 'MA10',
      type: 'line',
      data: ma10.value,
      smooth: true,
      lineStyle: { width: 1, color: chart.warn },
      symbol: 'none',
    },
    {
      name: 'MA20',
      type: 'line',
      data: ma20.value,
      smooth: true,
      lineStyle: { width: 1, color: chart.accent },
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
  ],
}))
</script>

<template>
  <v-chart class="chart" :option="option" :style="{ height: height ?? '420px' }" autoresize />
</template>

<style scoped>
.chart {
  width: 100%;
}
</style>
