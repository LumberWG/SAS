<script setup lang="ts">
/**
 * 扫描结果表：按股票聚合（同一只股票命中多个策略合并为一行）+ 表头排序 + 策略筛选 + CSV 导出。
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

/** 聚合后的一行 = 一只股票；strategies/reasons 保留它命中的所有策略。 */
type Row = {
  code: string
  name: string
  price: number | null
  /** 信号触发时的价格（扫描/记录时刻），与实时价区分 */
  signalPrice: number | null
  pct: number | null
  volRatio: number | null
  amountYi: number | null
  strategies: string[]
  reasons: string[]
  stopLoss: number | null
  targetPrice: number | null
  date: string
  best: ScanHit
}

const sortKey = ref<SortKey>('pct')
const sortDir = ref<'asc' | 'desc'>('desc')
const strategyFilter = ref('')

const strategyOptions = computed(() => [
  ...new Set(props.hits.map((h) => h.strategy)),
])

function numOf(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function scoreOf(hit: ScanHit): number {
  const s = hit.extra?.score
  return typeof s === 'number' ? s : -Infinity
}

/** 按股票代码聚合：一只股票一行，多策略合并。 */
const grouped = computed<Row[]>(() => {
  const map = new Map<string, ScanHit[]>()
  for (const hit of props.hits) {
    const list = map.get(hit.code)
    if (list) list.push(hit)
    else map.set(hit.code, [hit])
  }

  const rows: Row[] = []
  for (const [code, list] of map) {
    const best = list.reduce((a, b) => (scoreOf(b) > scoreOf(a) ? b : a))
    const first = list[0]
    // 价格/涨跌幅/量比/成交额以**当前实时行情**为准（后端每次请求按快照富化），
    // 没有实时快照时回退到命中时记录的值。
    const live = best.realtime ?? first.realtime ?? null
    rows.push({
      code,
      name: first.name,
      price: live?.price ?? best.price ?? first.price ?? null,
      signalPrice: best.price ?? first.price ?? null,
      pct: live?.pct_change ?? numOf(best.extra?.pct_change) ?? numOf(first.extra?.pct_change),
      volRatio: live?.volume_ratio ?? numOf(best.extra?.vol_ratio) ?? numOf(first.extra?.vol_ratio),
      amountYi: live?.amount_yi ?? numOf(best.extra?.amount_yi) ?? numOf(first.extra?.amount_yi),
      strategies: list.map((h) => h.strategy),
      reasons: list.map((h) => h.reason),
      stopLoss: best.stop_loss ?? null,
      targetPrice: best.target_price ?? null,
      date: best.date ?? first.date ?? '',
      best,
    })
  }
  return rows
})

const filtered = computed(() => {
  let rows = grouped.value
  if (strategyFilter.value) {
    rows = rows.filter((r) => r.strategies.includes(strategyFilter.value))
  }
  const key = sortKey.value
  const dir = sortDir.value === 'desc' ? -1 : 1
  return [...rows].sort((a, b) => {
    const va = num(a, key)
    const vb = num(b, key)
    return (va - vb) * dir
  })
})

function num(row: Row, key: SortKey): number {
  if (key === 'price') return row.price ?? 0
  if (key === 'pct') return row.pct ?? -Infinity
  if (key === 'vol_ratio') return row.volRatio ?? -Infinity
  return row.amountYi ?? -Infinity
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

/** 导出 CSV（带 BOM，Excel 直接打开不乱码）。一只股票一行，策略列用 / 连接。 */
function exportCsv() {
  const header = ['代码', '名称', '命中策略', '策略数', '最新价', '触发价', '涨跌幅%', '量比', '成交额(亿)', '止损价', '目标价', '命中原因']
  const lines = filtered.value.map((r) => [
    r.code,
    r.name,
    r.strategies.join(' / '),
    String(r.strategies.length),
    r.price?.toFixed(2) ?? '',
    r.signalPrice?.toFixed(2) ?? '',
    r.pct?.toFixed(2) ?? '',
    r.volRatio?.toFixed(2) ?? '',
    r.amountYi?.toFixed(2) ?? '',
    r.stopLoss?.toFixed(2) ?? '',
    r.targetPrice?.toFixed(2) ?? '',
    r.reasons.join('；'),
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
          <option value="">全部（{{ grouped.length }} 只 / {{ hits.length }} 信号）</option>
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
            <th
              class="cursor-pointer px-4 py-2.5 text-right font-medium hover:text-content"
              title="当前实时价（列表会自动刷新）；单元格悬停可看信号触发价"
              @click="toggleSort('price')"
            >
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
            v-for="(row, i) in filtered"
            :key="row.code"
            class="cursor-pointer transition-colors hover:bg-primary/10"
            :class="i % 2 === 1 ? 'bg-base-700/20' : ''"
            @click="emit('goDetail', row.best)"
          >
            <td class="font-num px-4 py-2.5 text-content-muted">{{ row.code }}</td>
            <td class="px-4 py-2.5 font-medium text-content">{{ row.name }}</td>
            <td class="px-1 py-2.5">
              <button
                class="rounded p-1 text-content-faint transition-colors hover:text-warn"
                title="加入自选股"
                @click.stop="emit('addWatch', row.best)"
              >
                <Star class="h-3.5 w-3.5" />
              </button>
            </td>
            <td
              class="font-num px-4 py-2.5 text-right text-content"
              :title="
                row.signalPrice != null && row.signalPrice !== row.price
                  ? `信号触发价 ${row.signalPrice.toFixed(2)}`
                  : ''
              "
            >
              {{ row.price?.toFixed(2) ?? '--' }}
            </td>
            <td class="font-num px-4 py-2.5 text-right font-medium" :class="pctClass(row.pct)">
              {{ fmtPct(row.pct) }}
            </td>
            <td class="font-num px-4 py-2.5 text-right text-content">
              {{ row.volRatio !== null ? row.volRatio.toFixed(2) : '--' }}
            </td>
            <td class="font-num px-4 py-2.5 text-right text-content">
              {{ row.amountYi !== null ? row.amountYi.toFixed(2) + '亿' : '--' }}
            </td>
            <td class="px-4 py-2.5">
              <span class="flex flex-wrap items-center gap-1">
                <span
                  v-for="s in row.strategies"
                  :key="s"
                  class="rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-primary-light"
                >
                  {{ s }}
                </span>
              </span>
            </td>
            <td class="max-w-[320px] truncate px-4 py-2.5 text-content-muted" :title="row.reasons.join('；')">
              {{ row.reasons[0] }}
              <span v-if="row.reasons.length > 1" class="text-content-faint">+{{ row.reasons.length - 1 }}</span>
            </td>
            <td class="font-num px-4 py-2.5 text-right text-down">{{ row.stopLoss?.toFixed(2) ?? '--' }}</td>
            <td class="font-num px-4 py-2.5 text-right text-up">{{ row.targetPrice?.toFixed(2) ?? '--' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
