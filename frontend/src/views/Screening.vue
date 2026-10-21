<script setup lang="ts">
/**
 * 选股中心：策略多选 + 扫描范围 → 触发扫描 → 进度轮询 → 命中结果。
 * 刷新页面后自动恢复最近一轮扫描结果。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Clock, Play, Search, Square, TrendingUp } from 'lucide-vue-next'

import {
  getLatestScan,
  getScanTask,
  getStrategies,
  startScan,
  type ScanHit,
  type ScanScope,
  type ScanTask,
  type StrategyInfo,
} from '../api'
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
      <div class="glass-card flex flex-wrap items-center gap-4 p-4">
        <button
          class="flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-primary-light px-5 py-2.5 text-[13px] font-semibold text-white shadow-glow transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="running || selected.length === 0"
          @click="start"
        >
          <Play class="h-4 w-4" />
          {{ running ? '扫描中...' : '开始扫描' }}
        </button>

        <div v-if="task" class="min-w-0 flex-1">
          <div class="mb-1.5 flex items-center justify-between text-[12px]">
            <span class="text-content-muted">
              <Search class="mr-1 inline h-3.5 w-3.5" />
              {{ task.status === 'done' ? '扫描完成' : task.status === 'error' ? '扫描中断' : '正在扫描' }}
              <span v-if="task.total"> · {{ task.scanned }}/{{ task.total }} 只</span>
              <span v-if="task.skipped"> · 跳过 {{ task.skipped }} 只</span>
              <span v-if="task.duration !== null" class="text-content-faint">
                <Clock class="ml-1 inline h-3 w-3" />{{ fmtDuration(task.duration) }}
              </span>
            </span>
            <span class="font-num text-content">{{ task.progress }}%</span>
          </div>
          <div class="h-2 overflow-hidden rounded-full bg-base-700">
            <div
              class="h-full rounded-full bg-gradient-to-r from-primary to-primary-light transition-all duration-500"
              :style="{ width: `${task.progress}%` }"
            />
          </div>
        </div>

        <div v-if="task && task.status === 'done'" class="flex shrink-0 items-center gap-3">
          <div class="text-right">
            <div class="font-num text-[20px] font-semibold text-primary-light">{{ task.hit_count }}</div>
            <div class="text-[11px] text-content-faint">命中信号</div>
          </div>
          <div class="text-right">
            <div class="font-num text-[13px] font-semibold text-up">
              {{ task.hits.filter((h) => typeof h.extra?.pct_change === 'number' && (h.extra.pct_change as number) > 0).length }}
            </div>
            <div class="text-[11px] text-content-faint">
              <TrendingUp class="inline h-3 w-3" />收阳
            </div>
          </div>
          <!-- 本轮扫描市场环境 -->
          <div
            v-if="task.sentiment?.level"
            class="rounded-lg border px-3 py-1.5 text-center"
            :class="
              task.sentiment.weight > 0
                ? 'border-up/40 bg-up/10'
                : task.sentiment.weight < 0
                  ? 'border-down/40 bg-down/10'
                  : 'border-base-600 bg-base-700/60'
            "
            :title="task.sentiment.advice"
          >
            <div class="text-[12px] font-semibold" :class="task.sentiment.weight > 0 ? 'text-up' : task.sentiment.weight < 0 ? 'text-down' : 'text-content'">
              {{ task.sentiment.level }}
              <span class="font-num">{{ task.sentiment.weight > 0 ? '+' : '' }}{{ task.sentiment.weight }}</span>
            </div>
            <div class="text-[10px] text-content-faint">扫描时市场情绪</div>
          </div>
        </div>
      </div>

      <!-- 结果表 -->
      <ScanResultTable
        v-if="task && task.hits.length > 0"
        :hits="task.hits"
        @go-detail="goDetail"
      />

      <!-- 空状态 -->
      <div v-else class="glass-card flex min-h-0 flex-1 flex-col items-center justify-center gap-3 text-content-faint">
        <Square class="h-10 w-10 opacity-30" />
        <p class="text-[13px]">
          {{
            task?.status === 'done'
              ? '本轮扫描没有命中任何策略'
              : task?.status === 'error'
                ? '上一轮扫描被中断（可能后端重启），请重新开始扫描'
                : '选择策略与范围后点击「开始扫描」'
          }}
        </p>
        <p class="text-[11px]">
          冷缓存全市场扫描约需数分钟；选择「成交额前 500」范围可快速获得结果。命中信号自动存入信号库
        </p>
      </div>
    </section>
  </div>
</template>
