<script setup lang="ts">
/**
 * 六阶段生命周期视图（#130）。
 *
 * 把「初选 → 入自选 → 进交易队列 → 入场 → 持有 → 离场」这条唯一正常链路做成一个
 * 只读可视化页：顶部六张阶段卡展示每个阶段累计事件数，下方时间线按时间倒序展示
 * 每一次状态跃迁（含越级旁路的 event 编码），并支持按股票代码 / 阶段过滤。
 *
 * 设计：沿用全站语义色（红涨绿跌、accent 紫、primary 蓝、warn 橙），时间线用
 * Tailwind 自研（无 element-plus），不引入任何色值字面量，主题切换自动跟随。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import MetricCard from '../components/MetricCard.vue'
import {
  getLifecycleEvents,
  getLifecycleStages,
  type LifecycleEvent,
  type LifecycleStage,
} from '../api'
import { POLL } from '../lib/polling'

// 阶段序号 → 语义色（字面类，保证 Tailwind 不 purge）
const STAGE_TONE: Record<number, string> = {
  1: 'text-accent',
  2: 'text-primary-light',
  3: 'text-warn',
  4: 'text-up',
  5: 'text-content-muted',
  6: 'text-down',
}
const STAGE_DOT: Record<number, string> = {
  1: 'bg-accent',
  2: 'bg-primary',
  3: 'bg-warn',
  4: 'bg-up',
  5: 'bg-content-muted',
  6: 'bg-down',
}
// 阶段中文名（与时间线/概览卡一致）
const STAGE_NAMES: Record<number, string> = {
  1: '初选',
  2: '入自选',
  3: '进交易队列',
  4: '入场',
  5: '持有',
  6: '离场',
}
// 阶段序号 → 圆角序号徽标（①②…），与中文名配对，一眼定位链路位置
const STAGE_BADGE = ['', '①', '②', '③', '④', '⑤', '⑥']

const stages = ref<LifecycleStage[]>([])
const events = ref<LifecycleEvent[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const codeFilter = ref('')
const stageFilter = ref<number | null>(null)

const stageMap = computed<Record<number, LifecycleStage>>(() => {
  const m: Record<number, LifecycleStage> = {}
  for (const s of stages.value) m[s.stage] = s
  return m
})

const sortedStages = computed<LifecycleStage[]>(() =>
  [1, 2, 3, 4, 5, 6].map(
    (n) => stageMap.value[n] ?? { stage: n, stage_name: STAGE_NAMES[n], cnt: 0 }
  )
)

const filteredEvents = computed<LifecycleEvent[]>(() => {
  let list = events.value
  if (stageFilter.value != null) list = list.filter((e) => e.stage === stageFilter.value)
  return list
})

async function loadStages() {
  try {
    stages.value = (await getLifecycleStages()).stages
  } catch (e) {
    error.value = `加载阶段概览失败：${String(e)}`
  }
}

async function loadEvents() {
  loading.value = true
  error.value = null
  try {
    const code = codeFilter.value.trim() || undefined
    events.value = (await getLifecycleEvents(code, 300)).events
  } catch (e) {
    error.value = `加载事件失败：${String(e)}`
  } finally {
    loading.value = false
  }
}

function refresh() {
  loadStages()
  loadEvents()
}

function onStageCardClick(stage: number) {
  stageFilter.value = stageFilter.value === stage ? null : stage
}

function clearFilters() {
  codeFilter.value = ''
  stageFilter.value = null
  loadEvents()
}

function fmtTime(ts: string): string {
  if (!ts) return ''
  // 后端 created_at 多为 ISO（含空格分隔），统一显示为「MM-DD HH:mm:ss」
  const s = ts.replace('T', ' ').replace(/\.\d+$/, '').slice(0, 19)
  return s
}

let timer: number | undefined
onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, POLL.defaultMs)
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="mx-auto max-w-[1180px] space-y-5 px-4 py-5">
    <!-- 标题区 -->
    <header class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 class="text-heading text-content">六阶段生命周期</h1>
        <p class="mt-1 text-body text-content-muted">
          初选 → 入自选 → 进交易队列 → 入场 → 持有 → 离场：一只票从被系统发现到离场的完整旅程，
          以及每一次越级旁路（event 编码）都可在此核对。
        </p>
      </div>
      <button class="btn btn-ghost" :disabled="loading" @click="refresh">
        {{ loading ? '刷新中…' : '刷新' }}
      </button>
    </header>

    <!-- 六阶段卡 -->
    <section class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <MetricCard
        v-for="s in sortedStages"
        :key="s.stage"
        :title="`${STAGE_BADGE[s.stage]} ${s.stage_name}`"
        :value="s.cnt"
        :sub="s.stage === 5 ? '隐含态（入场→离场之间）' : '累计事件'"
        :clickable="true"
        @click="onStageCardClick(s.stage)"
      >
        <span
          v-if="stageFilter === s.stage"
          class="mt-2 inline-block rounded-control bg-primary/15 px-2 py-0.5 text-[11px] text-primary-light"
        >已筛选</span>
      </MetricCard>
    </section>

    <!-- 过滤条 -->
    <section class="glass-card flex flex-wrap items-center gap-3 p-3">
      <input
        v-model="codeFilter"
        class="field w-40"
        placeholder="股票代码筛选（如 600000）"
        @keyup.enter="loadEvents"
      />
      <button class="btn btn-primary" :disabled="loading" @click="loadEvents">按代码查询</button>
      <button
        v-if="codeFilter || stageFilter != null"
        class="btn btn-ghost"
        @click="clearFilters"
      >
        清除筛选
      </button>
      <span class="ml-auto text-caption text-content-faint">
        共 {{ filteredEvents.length }} 条事件
        <template v-if="stageFilter != null">（阶段筛选：{{ STAGE_NAMES[stageFilter] }}）</template>
      </span>
    </section>

    <!-- 错误提示 -->
    <div v-if="error" class="glass-card border border-down/40 p-3 text-body text-down">
      {{ error }}
    </div>

    <!-- 时间线 -->
    <section class="glass-card p-4">
      <h2 class="mb-4 text-subheading text-content">事件时间线（倒序）</h2>
      <div v-if="filteredEvents.length === 0" class="py-10 text-center text-body text-content-faint">
        暂无生命周期事件。系统跑过扫描 / 自动加自选 / 交易队列 / 模拟成交后，这里会逐步积累。
      </div>
      <ol v-else class="relative space-y-3 border-l border-base-600 pl-5">
        <li
          v-for="ev in filteredEvents"
          :key="ev.id"
          class="relative"
        >
          <!-- 阶段节点圆点 -->
          <span
            class="absolute -left-[27px] top-1.5 h-3.5 w-3.5 rounded-full ring-2 ring-base-800"
            :class="STAGE_DOT[ev.stage] || 'bg-content-muted'"
          ></span>
          <div class="glass-card flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
            <span
              class="rounded-control px-2 py-0.5 text-[12px] font-medium"
              :class="[STAGE_TONE[ev.stage] || 'text-content-muted', 'bg-base-700/60']"
            >
              {{ STAGE_BADGE[ev.stage] }} {{ ev.stage_name }}
            </span>
            <span class="font-num text-[12.5px] text-content-muted">{{ ev.event }}</span>
            <span class="ml-auto font-num text-[12px] text-content-faint">{{ fmtTime(ev.created_at) }}</span>
            <div class="w-full flex flex-wrap items-center gap-2 pt-0.5">
              <span class="font-num text-[13px] font-semibold text-content">{{ ev.code }}</span>
              <span class="text-[12.5px] text-content-muted">{{ ev.name }}</span>
              <span v-if="ev.detail" class="text-[12.5px] text-content-faint">· {{ ev.detail }}</span>
            </div>
          </div>
        </li>
      </ol>
    </section>
  </div>
</template>
