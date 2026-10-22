<script setup lang="ts">
/**
 * 选股中心：策略多选 + 扫描范围 → 触发扫描 → 进度轮询 → 命中结果。
 * 刷新页面后自动恢复最近一轮扫描结果。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { AlertCircle, CheckCircle2, Clock, Play, Search, Square, TrendingUp } from 'lucide-vue-next'

import {
  addWatch,
  getLatestScan,
  getScanTask,
  getStrategies,
  startScan,
  type ScanHit,
  type ScanScope,
  type ScanTask,
  type StrategyInfo,
} from '../api'
import ScanEmptyState from '../components/ScanEmptyState.vue'
import ScanResultTable from '../components/ScanResultTable.vue'
import StrategyEditor from '../components/StrategyEditor.vue'
import StrategyPanel from '../components/StrategyPanel.vue'

const router = useRouter()

const strategies = ref<StrategyInfo[]>([])
const scopes = ref<ScanScope[]>([])
const todayHits = ref<Record<string, number>>({})
const selected = ref<string[]>([])
const scope = ref('all')
const task = ref<ScanTask | null>(null)
const editingStrategy = ref<StrategyInfo | null>(null)
let poller: number | undefined

const running = computed(() => task.value?.status === 'running' || task.value?.status === 'pending')

// 今日历史命中总数（信号库统计，用于空状态上下文提示）
const todayHitTotal = computed(() =>
  Object.values(todayHits.value).reduce((sum, n) => sum + n, 0)
)

// 本轮命中中收阳的数量
const upCount = computed(
  () =>
    task.value?.hits.filter(
      (h) =>
        typeof h.extra?.pct_change === 'number' && (h.extra.pct_change as number) > 0
    ).length ?? 0
)

// 当前扫描范围的显示名
const scopeLabel = computed(
  () => scopes.value.find((s) => s.key === scope.value)?.label ?? scope.value
)

async function loadStrategies() {
  const data = await getStrategies()
  strategies.value = data.strategies
  scopes.value = data.scopes
  todayHits.value = data.today_hits
  // 默认全选（新增自定义策略自动勾选）
  selected.value = data.strategies.map((s) => s.name)
}

/** 自定义策略保存/删除后：刷新列表 + 自动勾选新策略。 */
async function onStrategySaved(name: string) {
  editingStrategy.value = null
  await loadStrategies()
  if (!selected.value.includes(name)) {
    selected.value.push(name)
  }
}

async function onStrategyDeleted(name: string) {
  editingStrategy.value = null
  selected.value = selected.value.filter((n) => n !== name)
  await loadStrategies()
}

/** 恢复最近一轮扫描（刷新页面不丢结果）。 */
async function restoreLatest() {
  try {
    const { task: latest } = await getLatestScan()
    if (latest) {
      task.value = latest
      if (latest.status === 'running') {
        // 后端重启会导致任务中断，标记为失败态由用户重扫
        task.value = { ...latest, status: 'error' }
      }
    }
  } catch {
    // 恢复失败静默（首次使用无历史任务）
  }
}

function toggleStrategy(name: string) {
  if (selected.value.includes(name)) {
    selected.value = selected.value.filter((n) => n !== name)
  } else {
    selected.value.push(name)
  }
}

async function start() {
  if (selected.value.length === 0) return
  const { task_id } = await startScan(selected.value, undefined, scope.value)
  task.value = await getScanTask(task_id)
  // 2 秒轮询任务状态
  poller = window.setInterval(async () => {
    if (!task.value) return
    task.value = await getScanTask(task.value.task_id)
    if (task.value.status === 'done' || task.value.status === 'error') {
      stopPolling()
    }
  }, 2000)
}

function stopPolling() {
  if (poller) {
    window.clearInterval(poller)
    poller = undefined
  }
}

onUnmounted(stopPolling)

function goDetail(hit: ScanHit) {
  router.push(`/stock/${hit.code}`)
}

/** 从扫描结果直接加自选（409=已在自选股中，均给出反馈）。 */
async function addToWatch(hit: ScanHit) {
  try {
    await addWatch(hit.code)
    toast(`已将 ${hit.name} 加入自选`)
  } catch (e: unknown) {
    const status = (e as { response?: { status?: number } })?.response?.status
    const detail = (e as { response?: { data?: { detail?: string } } })?.response
      ?.data?.detail
    if (status === 409) {
      toast(`${hit.name} 已在自选股中`)
    } else {
      toast(detail ?? '添加自选失败')
    }
  }
}

// 轻量操作反馈（避免引入完整 toast 组件）
const toastMsg = ref('')
let toastTimer: number | undefined
function toast(msg: string) {
  toastMsg.value = msg
  if (toastTimer) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => (toastMsg.value = ''), 2500)
}

function fmtDuration(seconds: number | null): string {
  if (seconds === null) return ''
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

onMounted(async () => {
  await Promise.all([loadStrategies(), restoreLatest()])
})
</script>

<template>
  <div class="flex h-[calc(100vh-8rem)] gap-5">
    <!-- 左侧策略面板 -->
    <StrategyPanel
      :strategies="strategies"
      :selected="selected"
      :today-hits="todayHits"
      :scopes="scopes"
      :scope="scope"
      :running="running"
      @toggle="toggleStrategy"
      @select-all="selected = strategies.map((s) => s.name)"
      @clear-all="selected = []"
      @edit="editingStrategy = $event"
      @update:scope="scope = $event"
    />

    <!-- 策略编辑器（模态） -->
    <StrategyEditor
      v-if="editingStrategy"
      :strategy="editingStrategy"
      @close="editingStrategy = null"
      @saved="onStrategySaved"
      @deleted="onStrategyDeleted"
    />

    <!-- 右侧扫描区 -->
    <section class="flex min-w-0 flex-1 flex-col gap-4">
      <!-- 操作栏 -->
      <div class="glass-card p-4">
        <div class="flex flex-wrap items-center gap-4">
          <!-- 主操作按钮 -->
          <button
            class="group flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-primary-light px-5 py-2.5 text-[13px] font-semibold text-white shadow-glow transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="running || selected.length === 0"
            @click="start"
          >
            <Play class="h-4 w-4 transition-transform group-hover:scale-110" />
            {{ running ? '扫描中...' : '开始扫描' }}
          </button>

          <!-- 当前策略/范围摘要 -->
          <div class="hidden min-w-0 flex-1 text-[12px] text-content-faint md:block">
            <span class="text-content-muted">{{ selected.length }}</span> 个策略 ·
            <span class="text-content-muted">{{ scopeLabel }}</span>
          </div>

          <!-- 进度区 -->
          <div v-if="task" class="min-w-0 flex-1">
            <div class="mb-1.5 flex items-center justify-between text-[12px]">
              <span class="flex items-center gap-1.5 text-content-muted">
                <Search v-if="task.status === 'running'" class="h-3.5 w-3.5 animate-pulse text-primary-light" />
                <CheckCircle2 v-else-if="task.status === 'done'" class="h-3.5 w-3.5 text-up" />
                <AlertCircle v-else-if="task.status === 'error'" class="h-3.5 w-3.5 text-up" />
                {{ task.status === 'done' ? '扫描完成' : task.status === 'error' ? '扫描中断' : '正在扫描' }}
                <span v-if="task.total" class="font-num">· {{ task.scanned }}/{{ task.total }} 只</span>
                <span v-if="task.skipped" class="font-num">· 跳过 {{ task.skipped }}</span>
                <span v-if="task.duration !== null" class="text-content-faint">
                  <Clock class="ml-1 inline h-3 w-3" />{{ fmtDuration(task.duration) }}
                </span>
              </span>
              <span class="font-num text-content">{{ task.progress }}%</span>
            </div>
            <div class="relative h-2 overflow-hidden rounded-full bg-base-700">
              <div
                class="relative h-full rounded-full bg-gradient-to-r from-primary to-primary-light transition-all duration-500"
                :class="task.status === 'running' ? 'progress-shimmer overflow-hidden' : ''"
                :style="{ width: `${task.progress}%` }"
              />
            </div>
          </div>
        </div>

        <!-- 完成统计（仅 done 展示，分区排列） -->
        <div
          v-if="task && task.status === 'done'"
          class="mt-4 grid grid-cols-2 gap-3 border-t border-base-600/50 pt-4 sm:grid-cols-4"
        >
          <div class="rounded-lg bg-base-700/40 p-3 text-center">
            <div class="font-num bg-gradient-to-r from-primary-light to-primary bg-clip-text text-[24px] font-bold leading-none text-transparent">
              {{ task.hit_count }}
            </div>
            <div class="mt-1 text-[11px] text-content-faint">命中信号</div>
          </div>
          <div class="rounded-lg bg-base-700/40 p-3 text-center">
            <div class="font-num text-[24px] font-bold leading-none text-up">
              {{ upCount }}
            </div>
            <div class="mt-1 text-[11px] text-content-faint">
              <TrendingUp class="inline h-3 w-3" /> 收阳
            </div>
          </div>
          <div class="rounded-lg bg-base-700/40 p-3 text-center">
            <div class="font-num text-[24px] font-bold leading-none text-content">
              {{ task.total }}
            </div>
            <div class="mt-1 text-[11px] text-content-faint">扫描股票</div>
          </div>
          <!-- 扫描时市场情绪 -->
          <div
            v-if="task.sentiment?.level"
            class="rounded-lg border p-3 text-center"
            :class="
              task.sentiment.weight > 0
                ? 'border-up/30 bg-up/10'
                : task.sentiment.weight < 0
                  ? 'border-down/30 bg-down/10'
                  : 'border-base-600 bg-base-700/40'
            "
            :title="task.sentiment.advice"
          >
            <div
              class="text-[15px] font-semibold leading-none"
              :class="task.sentiment.weight > 0 ? 'text-up' : task.sentiment.weight < 0 ? 'text-down' : 'text-content'"
            >
              {{ task.sentiment.level }}
            </div>
            <div class="font-num mt-1 text-[11px] text-content-faint">
              权重 {{ task.sentiment.weight > 0 ? '+' : '' }}{{ task.sentiment.weight }} · 扫描时情绪
            </div>
          </div>
        </div>
      </div>

      <!-- 结果表（v-else 必须与下一个空状态相邻，中间不能夹其他元素） -->
      <ScanResultTable
        v-if="task && task.hits.length > 0"
        :hits="task.hits"
        @go-detail="goDetail"
        @add-watch="addToWatch"
      />

      <!-- 空状态（本轮无命中 / 扫描中断 / 未开始） -->
      <ScanEmptyState v-else :status="task?.status ?? null" :today-hit-total="todayHitTotal" />
    </section>

    <!-- 轻量 toast 反馈（独立放置，避免插入 v-if/v-else 之间导致配对错位） -->
    <div
      v-if="toastMsg"
      class="fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-primary/40 bg-base-800/95 px-4 py-2 text-[13px] text-content shadow-card backdrop-blur"
    >
      {{ toastMsg }}
    </div>
  </div>
</template>
