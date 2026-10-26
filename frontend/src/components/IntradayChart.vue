<script setup lang="ts">
/**
 * ECharts 分时图：价格线 + 当日均价 + 昨收基准线 + 成交量副图。
 * A股配色：高于昨收红（#F5222D）、低于昨收绿（#00B96B）。
 */
import { computed } from 'vue'
import VChart from 'vue-echarts'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, BarChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
} from 'echarts/components'

use([
  CanvasRenderer,
  LineChart,
  BarChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  MarkLineComponent,
])

const props = defineProps<{
  times: string[]
  prices: (number | null)[]
  avgPrices: (number | null)[]
  volumes: number[]
  prevClose?: number | null
  height?: string
}>()

const UP = '#F5222D'
const DOWN = '#00B96B'
const NEUTRAL = '#38BDF8'

const base = computed(() =>
  props.prevClose != null && props.prevClose > 0 ? props.prevClose : null,
)

const validPrices = computed(() =>
  props.prices.filter((p): p is number => p != null && p > 0),
)

const lastPrice = computed(() =>
  validPrices.value.length ? validPrices.value[validPrices.value.length - 1] : null,
)

const tone = computed(() => {
  if (base.value == null || lastPrice.value == null) return NEUTRAL
  if (lastPrice.value > base.value) return UP
  if (lastPrice.value < base.value) return DOWN
  return '#8B9BB4'
})

/** 以昨收为中心对称取值域：涨跌视觉幅度一致（与行情软件一致）。 */
const yRange = computed(() => {
  const prev = base.value
  const values = [
    ...validPrices.value,
    ...props.avgPrices.filter((p): p is number => p != null && p > 0),
  ]
  if (!values.length) return null
  if (prev == null) {
    return {
      min: Math.min(...values) * 0.999,
      max: Math.max(...values) * 1.001,
    }
  }
  const dev = Math.max(prev * 0.004, ...values.map((v) => Math.abs(v - prev))) * 1.08
  return { min: prev - dev, max: prev + dev }
})

/** 成交量柱按「较上一分钟涨跌」着色。 */
const volumeBars = computed(() =>
  props.volumes.map((v, i) => {
    const cur = props.prices[i]
    let before: number | null = base.value
    for (let j = i - 1; j >= 0; j--) {
      const p = props.prices[j]
      if (p != null) {
        before = p
        break
      }
    }
    const up = cur != null && before != null ? cur >= before : true
    return { value: v, itemStyle: { color: up ? UP : DOWN } }
  }),
)

/** 横轴标签密度：240 根时约 5 个刻度。 */
const labelInterval = computed(() =>
  Math.max(0, Math.ceil(props.times.length / 5) - 1),
)

const option = computed(() => ({
  animation: false,
  backgroundColor: 'transparent',
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'cross', lineStyle: { color: '#5C6B85' } },
    backgroundColor: '#111A2E',
    borderColor: '#243354',
    textStyle: { color: '#E6EDF7', fontSize: 12 },
  },
  legend: {
    data: ['价格', '均价'],
    textStyle: { color: '#8B9BB4', fontSize: 11 },
    top: 0,
    right: 0,
    icon: 'roundRect',
    itemWidth: 12,
    itemHeight: 3,
  },
  grid: [
    { left: 56, right: 14, top: 26, height: '58%' },
    { left: 56, right: 14, top: '74%', height: '16%' },
  ],
  xAxis: [
    {
      type: 'category',
      data: props.times,
      boundaryGap: false,
      axisLine: { lineStyle: { color: '#243354' } },
      axisTick: { show: false },
      axisLabel: { show: false },
    },
    {
      type: 'category',
      gridIndex: 1,
      data: props.times,
      boundaryGap: false,
      axisLine: { lineStyle: { color: '#243354' } },
      axisTick: { show: false },
      axisLabel: {
        color: '#5C6B85',
        fontSize: 10,
        interval: labelInterval.value,
      },
    },
  ],
  yAxis: [
    {
      scale: true,
      min: yRange.value?.min,
      max: yRange.value?.max,
      axisLabel: {
        color: '#5C6B85',
        fontSize: 10,
        formatter: (v: number) => v.toFixed(2),
      },
      splitLine: { lineStyle: { color: 'rgba(36,51,84,0.6)' } },
    },
    {
      scale: true,
      gridIndex: 1,
      axisLabel: { show: false },
      splitLine: { show: false },
    },
  ],
  series: [
    {
      name: '价格',
      type: 'line',
      data: props.prices,
      symbol: 'none',
      lineStyle: { width: 1.4, color: tone.value },
      itemStyle: { color: tone.value },
      areaStyle: { color: 'rgba(56,189,248,0.12)' },
      markLine:
        base.value != null
          ? {
              symbol: 'none',
              silent: true,
              lineStyle: { color: '#8B9BB4', type: 'dashed', width: 1 },
              label: {
                position: 'end',
                color: '#8B9BB4',
                fontSize: 10,
                formatter: '昨收',
              },
              data: [{ yAxis: base.value }],
            }
          : undefined,
    },
    {
      name: '均价',
      type: 'line',
      data: props.avgPrices,
      symbol: 'none',
      lineStyle: { width: 1, color: '#FAAD14' },
      itemStyle: { color: '#FAAD14' },
    },
    {
      name: '成交量',
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: volumeBars.value,
      barWidth: '60%',
    },
  ],
}))
</script>

<template>
  <v-chart class="chart" :option="option" :style="{ height: height ?? '360px' }" autoresize />
</template>

<style scoped>
.chart {
  width: 100%;
}
</style>
