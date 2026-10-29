<script setup lang="ts">
/**
 * 选股中心：策略多选 + 扫描范围 → 触发扫描 → 进度轮询 → 命中结果。
 * 刷新页面后自动恢复最近一轮扫描结果。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { AlertCircle, CheckCircle2, Clock, Database, Play, Search, Square, TrendingUp } from 'lucide-vue-next'

import {
  addWatch,
  getWatchlist,
  getLatestScan,
  getScanTask,
  getStrategies,
  startScan,
  warmupKlines,
  getKlineStatus,
  type KlineStatus,
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
// 已在自选股里的代码（二选结果）：用于在股票池里标注「已进自选 / 未进」
const watchCodes = ref<string[]>([])

async function loadWatchCodes() {
  try {
    const data = await getWatchlist()
    watchCodes.value = data.items.map((it) => it.code)
  } catch {
    watchCodes.value = []
  }
}
const selected = ref<string[]>([])
const scope = ref('top_amount:500')
const task = ref<ScanTask | null>(null)
const editingStrategy = ref<StrategyInfo | null>(null)
const loadingStrategies = ref(true)
const loadError = ref('')
let poller: number | undefined

// 策略面板收起状态（窄屏或看结果时腾出宽度），记忆到本地
const panelCollapsed = ref(localStorage.getItem('screening.panelCollapsed') === '1')
watch(panelCollapsed, (v) => localStorage.setItem('screening.panelCollapsed', v ? '1' : '0'))

const running = computed(() => task.value?.status === 'running' || task.value?.status === 'pending')

// 扫描中每秒刷新一次的时钟：首只股票扫完前 scanned 一直是 0，
// 没有跳动的耗时会让人误以为进度卡死
const nowTick = ref(Date.now())
let ticker: number | undefined

function startTicker() {
  stopTicker()
  nowTick.value = Date.now()
  ticker = window.setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
}

function stopTicker() {
  if (ticker) {
    window.clearInterval(ticker)
    ticker = undefined
  }
}

/** 扫描已耗时（秒），running 时每秒跳动。 */
const elapsedSec = computed(() => {
  const t = task.value
  if (!t || t.status !== 'running' || !t.started_at) return 0
  const started = Date.parse(t.started_at.replace(' ', 'T'))
  if (Number.isNaN(started)) return 0
  return Math.max(0, Math.round((nowTick.value - started) / 1000))
})

/** 当前阶段提示：代码池准备中 / 首批K线拉取中（进度为 0 时说明在干什么）。 */
const phaseHint = computed(() => {
  const t = task.value
  if (!t || t.status !== 'running') return ''
  if (!t.total) return '正在准备股票池…'
  if (!t.scanned) return '正在拉取K线（首批完成后开始计数）…'
  return ''
})

// 今日历史命中总数（信号库统计，用于空状态上下文提示）
const todayHitTotal = computed(() =>
  Object.values(todayHits.value).reduce((sum, n) => sum + n, 0)
)

// 本轮命中的股票只数（一只股票可能命中多个策略，这里按代码去重）
const hitStockCount = computed(
  () => new Set((task.value?.hits ?? []).map((h) => h.code)).size
)

// 本轮命中中收阳的股票只数（按代码去重，与结果表一行一股保持一致）
const upCount = computed(() => {
  const upCodes = new Set<string>()
  for (const h of task.value?.hits ?? []) {
    const pct = h.realtime?.pct_change ?? h.extra?.pct_change
    if (typeof pct === 'number' && pct > 0) upCodes.add(h.code)
  }
  return upCodes.size
})

// 当前扫描范围的显示名
const scopeLabel = computed(
  () => scopes.value.find((s) => s.key === scope.value)?.label ?? scope.value
)

// K线缓存覆盖状态（预热进度）
const klineStatus = ref<KlineStatus | null>(null)
let statusPoller: number | undefined

async function loadKlineStatus() {
  try {
    klineStatus.value = await getKlineStatus()
  } catch {
    // 状态获取失败静默
  }
}

function stopStatusPolling() {
  if (statusPoller) {
    window.clearInterval(statusPoller)
    statusPoller = undefined
  }
}

async function loadStrategies() {
  loadError.value = ''
  loadingStrategies.value = true
  try {
    const data = await getStrategies()
    strategies.value = data.strategies
    scopes.value = data.scopes
    todayHits.value = data.today_hits
    // 默认全选（新增自定义策略自动勾选）
    selected.value = data.strategies.map((s) => s.name)
  } catch {
    // 策略加载失败（后端未就绪/网络异常）：保留错误态供用户重试，
    // 避免按钮静默禁用看起来像"点击没反应"
    loadError.value = '策略列表加载失败：请确认后端服务已启动（http://127.0.0.1:8000），点击重试'
  } finally {
    loadingStrategies.value = false
  }
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
    if (!latest) return
    task.value = latest
    // 后端重启会清空内存任务，此时接口返回 null；
    // 能返回 running 说明任务确实在跑（刷新页面前点的扫描），继续轮询
    if (latest.status === 'running' || latest.status === 'pending') {
      startTicker()
      startPolling(latest.task_id)
    } else if (latest.status === 'done') {
      startQuoteRefresh(latest.task_id)
    }
  } catch {
    // 恢复失败静默（首次使用无历史任务）
  }
}

// 扫描完成后定时重取任务：后端每次请求都会按最新快照富化价格，
// 页面停留期间价格也保持实时（不改动扫描结果本身）。
let quotePoller: number | undefined

function startQuoteRefresh(taskId: string) {
  stopQuoteRefresh()
  quotePoller = window.setInterval(async () => {
    if (document.hidden) return
    try {
      task.value = await getScanTask(taskId)
    } catch {
      // 静默失败：保留当前展示，下次轮询再试
    }
  }, 30_000)
}

function stopQuoteRefresh() {
  if (quotePoller) {
    window.clearInterval(quotePoller)
    quotePoller = undefined
  }
}

/** 轮询扫描任务；任务丢失（如后端重启）时明确提示，不一直显示“扫描中”。 */
function startPolling(taskId: string) {
  stopPolling()
  let misses = 0
  poller = window.setInterval(async () => {
    try {
      const t = await getScanTask(taskId)
      misses = 0
      task.value = t
      if (t.status === 'done' || t.status === 'error') {
        stopPolling()
        stopTicker()
        loadKlineStatus()
        // 扫描完成时后端已执行「二选」（命中 Top N 自动加自选），刷新标注
        if (t.status === 'done') {
          void loadWatchCodes()
          startQuoteRefresh(taskId)
        }
      }
    } catch {
      misses += 1
      if (misses >= 3) {
        stopPolling()
        stopTicker()
        if (task.value) {
          task.value = {
            ...task.value,
            status: 'error',
            error: '任务已丢失（后端可能已重启），请重新扫描',
          }
        }
      }
    }
  }, 2000)
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
  try {
    stopQuoteRefresh()
    const { task_id } = await startScan(selected.value, undefined, scope.value)
    task.value = await getScanTask(task_id)
    startTicker()
    startPolling(task_id)
  } catch (e: unknown) {
    // 启动失败（400 未注册策略 / 500 等）：明确反馈，避免静默无响应
    const detail = (e as { response?: { data?: { detail?: string } } })?.response
      ?.data?.detail
    toast(detail ?? '扫描启动失败，请稍后重试')
  }
}

// 手动触发K线预热（后台批量缓存日K，加速后续扫描）
const warming = ref(false)
async function warmup() {
  if (warming.value) return
  warming.value = true
  try {
    await warmupKlines()
    toast('K线预热已在后台开始，完成后扫描将直接使用本地缓存')
    // 预热期间轮询进度，直到命中本地缓存比例接近满或预热按钮恢复
    stopStatusPolling()
    statusPoller = window.setInterval(loadKlineStatus, 3000)
  } catch {
    toast('预热触发失败，请稍后重试')
  } finally {
    warming.value = false
  }
}

function stopPolling() {
  if (poller) {
    window.clearInterval(poller)
    poller = undefined
  }
}

onUnmounted(() => {
  stopPolling()
  stopStatusPolling()
  stopQuoteRefresh()
  stopTicker()
})

function goDetail(hit: ScanHit) {
  router.push(`/stock/${hit.code}`)
}

/** 从扫描结果直接加自选（409=已在自选股中，均给出反馈）。 */
async function addToWatch(hit: ScanHit) {
  try {
    await addWatch(hit.code)
    void loadWatchCodes()
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
  await Promise.all([
    loadStrategies(),
    restoreLatest(),
    loadKlineStatus(),
    loadWatchCodes(),
  ])
})
</script>

<template>
  <!-- 仅本页做视口内固定高度，不改公共布局：
       可用高度 = 100dvh − 顶栏 56px − 内容区上下 padding 48px（共 6.5rem），
       多减 0.1rem 容错，避免亚像素溢出导致整页出现滚动条。
       操作栏 / 统计卡 / 工具行 / 表头固定，只有股票列表数据区滚动 -->
  <div class="flex h-[calc(100dvh-6.6rem)] min-h-0 min-w-0 gap-5 overflow-hidden">
    <!-- 左侧策略面板 -->
    <StrategyPanel
      v-model:collapsed="panelCollapsed"
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

    <!-- 右侧扫描区：操作栏固定，仅结果表内部滚动。
         w-0 作为收缩基准，避免内部宽表格把整页横向撑开 -->
    <section class="flex w-0 min-h-0 min-w-0 flex-1 flex-col gap-4">
      <!-- 策略加载失败提示（含重试） -->
      <div
        v-if="loadError"
        class="flex items-center justify-between gap-3 rounded-lg border border-down/40 bg-down/10 px-4 py-3 text-[12px] text-down"
      >
        <span>{{ loadError }}</span>
        <button
          class="shrink-0 rounded-md border border-down/40 px-2.5 py-1 text-[12px] transition-colors hover:bg-down/20"
          :disabled="loadingStrategies"
          @click="loadStrategies"
        >
          {{ loadingStrategies ? '加载中...' : '重试' }}
        </button>
      </div>

      <!-- 操作栏 -->
      <div class="glass-card shrink-0 p-4">
        <div class="flex flex-wrap items-center gap-4">
          <!-- 主操作按钮 -->
          <button
            class="flex items-center gap-1.5 rounded-lg border border-base-600 px-3 py-2.5 text-[12px] text-content-muted transition-colors hover:border-primary/50 hover:text-primary-light disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="running || warming"
            @click="warmup"
          >
            <Database class="h-3.5 w-3.5" />
            {{ warming ? '预热中...' : '预热K线' }}
          </button>
          <button
            class="group flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-primary-light px-5 py-2.5 text-[13px] font-semibold text-white shadow-glow transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="running || selected.length === 0"
            :title="selected.length === 0 ? '请先在左侧勾选至少一个策略' : ''"
            @click="start"
          >
            <Play class="h-4 w-4 transition-transform group-hover:scale-110" />
            {{ running ? '扫描中...' : '开始扫描' }}
          </button>

          <!-- 当前策略/范围摘要 -->
          <div class="hidden min-w-0 flex-1 text-[12px] text-content-faint md:block">
            <span class="text-content-muted">{{ selected.length }}</span> 个策略 ·
            <span class="text-content-muted">{{ scopeLabel }}</span>
            <span
              v-if="klineStatus"
              class="ml-2 rounded bg-base-700 px-1.5 py-0.5 font-num"
              :class="klineStatus.ratio >= 99 ? 'text-up' : 'text-content-muted'"
              :title="klineStatus.last_warmup?.at ? `最近预热: ${klineStatus.last_warmup.at}` : '尚未预热'"
            >
              本地K线 {{ klineStatus.cached }}/{{ klineStatus.total }} ({{ klineStatus.ratio }}%)
            </span>
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
                <span v-if="task.status === 'running' && elapsedSec > 0" class="font-num text-content-faint">
                  <Clock class="ml-1 inline h-3 w-3" />{{ fmtDuration(elapsedSec) }}
                </span>
                <span v-else-if="task.duration !== null" class="text-content-faint">
                  <Clock class="ml-1 inline h-3 w-3" />{{ fmtDuration(task.duration) }}
                </span>
                <span v-if="task.status === 'error' && task.error" class="text-down" :title="task.error">
                  · {{ task.error }}
                </span>
              </span>
              <span class="font-num text-content">{{ task.progress }}%</span>
            </div>
            <!-- 阶段提示：进度还是 0 的时候告诉用户在干什么，避免误以为卡死 -->
            <p v-if="phaseHint" class="mb-1.5 text-[11px] text-content-faint">
              {{ phaseHint }}
            </p>
            <div class="relative h-2 overflow-hidden rounded-full bg-base-700">
              <!-- 尚未产生进度：用往复滚动的不确定进度条表示仍在工作 -->
              <div
                v-if="task.status === 'running' && task.progress <= 0"
                class="progress-indeterminate absolute h-full rounded-full bg-gradient-to-r from-primary to-primary-light"
              />
              <div
                v-else
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
              {{ hitStockCount }}
            </div>
            <div class="mt-1 text-[11px] text-content-faint">
              命中股票<span class="text-content-faint/70">（{{ task.hit_count }} 信号）</span>
            </div>
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
        :watch-codes="watchCodes"
        @go-detail="goDetail"
        @add-watch="addToWatch"
      />

      <!-- 空状态（本轮无命中 / 扫描中断 / 未开始） -->
      <ScanEmptyState
        v-else
        :status="task?.status ?? null"
        :today-hit-total="todayHitTotal"
        :strategy-count="selected.length"
        :scope-label="scopeLabel"
        :kline-cached="klineStatus?.cached ?? 0"
        :kline-total="klineStatus?.total ?? 0"
        :kline-ratio="klineStatus?.ratio ?? 0"
      />
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
