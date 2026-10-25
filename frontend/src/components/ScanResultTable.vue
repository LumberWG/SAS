<script setup lang="ts">
/**
 * 扫描结果表：增强列（涨跌幅/量比/成交额）+ 表头排序 + 策略筛选 + CSV 导出。
 */
import { computed, ref } from 'vue'
import { ArrowDown, ArrowUp, Download, Star } from 'lucide-vue-next'

import type { ScanHit } from '../api'

const props = defineProps<{
  hits: ScanHit[]
}>()

const emit = defineEmits<{
  goDetail: [ScanHit]
  addWatch: [ScanHit]
}>()

type SortKey = 'price' | 'pct' | 'vol_ratio' | 'amount'
const sortKey = ref<SortKey>('pct')
const sortDir = ref<'asc' | 'desc'>('desc')
const strategyFilter = ref('')

const strategyOptions = computed(() => [
  ...new Set(props.hits.map((h) => h.strategy)),
])

const filtered = computed(() => {
  let rows = props.hits
  if (strategyFilter.value) {
    rows = rows.filter((h) => h.strategy === strategyFilter.value)
  }
  const key = sortKey.value
  const dir = sortDir.value === 'desc' ? -1 : 1
  return [...rows].sort((a, b) => {
    const va = num(a, key)
    const vb = num(b, key)
    return (va - vb) * dir
  })
})

function num(hit: ScanHit, key: SortKey): number {
  if (key === 'price') return hit.price ?? 0
  const v = hit.extra?.[key === 'pct' ? 'pct_change' : key === 'amount' ? 'amount_yi' : 'vol_ratio']
  return typeof v === 'number' ? v : -Infinity
}

function toggleSort(key: SortKey) {
  if (sortKey.value === key) {
    sortDir.value = sortDir.value === 'desc' ? 'asc' : 'desc'
  } else {
    sortKey.value = key
    sortDir.value = 'desc'
  }
}

function sortIcon(key: SortKey) {
  if (sortKey.value !== key) return null
  return sortDir.value === 'desc' ? ArrowDown : ArrowUp
}

function fmtPct(v: unknown): string {
  return typeof v === 'number' ? `${v > 0 ? '+' : ''}${v.toFixed(2)}%` : '--'
}

function pctClass(v: unknown): string {
  if (typeof v !== 'number' || v === 0) return 'text-content'
  return v > 0 ? 'text-up' : 'text-down'
}

/** 导出 CSV（带 BOM，Excel 直接打开不乱码）。 */
function exportCsv() {
  const header = ['代码', '名称', '策略', '最新价', '涨跌幅%', '量比', '成交额(亿)', '止损价', '目标价', '信号日期']
  const lines = filtered.value.map((h) => [
    h.code,
    h.name,
    h.strategy,
    h.price?.toFixed(2) ?? '',
    typeof h.extra?.pct_change === 'number' ? (h.extra.pct_change as number).toFixed(2) : '',
    typeof h.extra?.vol_ratio === 'number' ? (h.extra.vol_ratio as number).toFixed(2) : '',
    typeof h.extra?.amount_yi === 'number' ? (h.extra.amount_yi as number).toFixed(2) : '',
    h.stop_loss?.toFixed(2) ?? '',
    h.target_price?.toFixed(2) ?? '',
    h.date,
  ])
  const csv = '\uFEFF' + [header, ...lines].map((r) => r.join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `选股命中_${new Date().toISOString().slice(0, 10)}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="glass-card flex min-h-0 flex-1 flex-col overflow-hidden">
    <!-- 工具行：策略筛选 + 导出 -->
    <div class="flex items-center justify-between gap-3 border-b border-base-600/60 px-4 py-2.5">
      <div class="flex items-center gap-2">
        <span class="text-[11px] text-content-faint">策略筛选</span>
        <select
          v-model="strategyFilter"
          class="rounded-md border border-base-600 bg-base-700 px-2 py-1 text-[12px] text-content outline-none focus:border-primary"
        >
          <option value="">全部（{{ hits.length }}）</option>
          <option v-for="name in strategyOptions" :key="name" :value="name">
            {{ name }}（{{ hits.filter((h) => h.strategy === name).length }}）
          </option>
        </select>
      </div>
      <button
        class="flex items-center gap-1.5 rounded-md border border-base-600 px-2.5 py-1 text-[11px] text-content-muted transition-colors hover:border-primary/50 hover:text-primary-light"
        @click="exportCsv"
      >
        <Download class="h-3.5 w-3.5" />
        导出 CSV
      </button>
    </div>

    <!-- 表格：单元格不换行，列宽不足时横向滚动 -->
    <div class="min-h-0 flex-1 overflow-auto">
      <table class="w-full min-w-[1080px] whitespace-nowrap text-left text-[13px]">
        <thead class="sticky top-0 z-10 bg-base-800/95 text-[11px] text-content-faint backdrop-blur">
          <tr class="border-b border-base-600/60">
            <th class="px-4 py-2.5 font-medium">代码</th>
            <th class="px-4 py-2.5 font-medium">名称</th>
            <th class="px-1 py-2.5 font-medium" title="加入自选股">自选</th>
            <th class="cursor-pointer px-4 py-2.5 text-right font-medium hover:text-content" @click="toggleSort('price')">
              最新价
              <component :is="sortIcon('price')" v-if="sortIcon('price')" class="inline h-3 w-3" />
            </th>
            <th class="cursor-pointer px-4 py-2.5 text-right font-medium hover:text-content" @click="toggleSort('pct')">
              涨跌幅
              <component :is="sortIcon('pct')" v-if="sortIcon('pct')" class="inline h-3 w-3" />
            </th>
            <th class="cursor-pointer px-4 py-2.5 text-right font-medium hover:text-content" @click="toggleSort('vol_ratio')">
              量比
              <component :is="sortIcon('vol_ratio')" v-if="sortIcon('vol_ratio')" class="inline h-3 w-3" />
            </th>
            <th class="cursor-pointer px-4 py-2.5 text-right font-medium hover:text-content" @click="toggleSort('amount')">
              成交额
              <component :is="sortIcon('amount')" v-if="sortIcon('amount')" class="inline h-3 w-3" />
            </th>
            <th class="px-4 py-2.5 font-medium">命中策略</th>
            <th class="px-4 py-2.5 font-medium">命中原因</th>
            <th class="px-4 py-2.5 text-right font-medium">止损价</th>
            <th class="px-4 py-2.5 text-right font-medium">目标价</th>
          </tr>
        </thead>
        <tbody>
            <tr
              v-for="(hit, i) in filtered"
              :key="hit.code + hit.strategy"
              class="cursor-pointer transition-colors hover:bg-primary/10"
              :class="i % 2 === 1 ? 'bg-base-700/20' : ''"
              @click="emit('goDetail', hit)"
            >
            <td class="font-num px-4 py-2.5 text-content-muted">{{ hit.code }}</td>
            <td class="px-4 py-2.5 font-medium text-content">{{ hit.name }}</td>
            <td class="px-1 py-2.5">
              <button
                class="rounded p-1 text-content-faint transition-colors hover:text-warn"
                title="加入自选股"
                @click.stop="emit('addWatch', hit)"
              >
                <Star class="h-3.5 w-3.5" />
              </button>
            </td>
            <td class="font-num px-4 py-2.5 text-right text-content">{{ hit.price?.toFixed(2) }}</td>
            <td class="font-num px-4 py-2.5 text-right font-medium" :class="pctClass(hit.extra?.pct_change)">
              {{ fmtPct(hit.extra?.pct_change) }}
            </td>
            <td class="font-num px-4 py-2.5 text-right text-content">
              {{ typeof hit.extra?.vol_ratio === 'number' ? (hit.extra.vol_ratio as number).toFixed(2) : '--' }}
            </td>
            <td class="font-num px-4 py-2.5 text-right text-content">
              {{ typeof hit.extra?.amount_yi === 'number' ? (hit.extra.amount_yi as number).toFixed(2) + '亿' : '--' }}
            </td>
            <td class="px-4 py-2.5">
              <span class="rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-primary-light">
                {{ hit.strategy }}
              </span>
            </td>
            <td class="max-w-[240px] truncate px-4 py-2.5 text-content-muted" :title="hit.reason">
              {{ hit.reason }}
            </td>
            <td class="font-num px-4 py-2.5 text-right text-down">{{ hit.stop_loss?.toFixed(2) ?? '--' }}</td>
            <td class="font-num px-4 py-2.5 text-right text-up">{{ hit.target_price?.toFixed(2) ?? '--' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
