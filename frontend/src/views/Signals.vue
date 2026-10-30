<script setup lang="ts">
/**
 * 信号与自选股：
 *  - 自选股分「程序自选」（扫描命中自动写入）与「手动自选」（用户添加）两个标签
 *  - 程序自选可勾选后批量转入手动自选
 *  - 历史信号流水
 */
import { computed, onActivated, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowRightLeft,
  Bell,
  CheckSquare,
  Plus,
  Sparkles,
  Square,
  Star,
  Trash2,
} from 'lucide-vue-next'

import http, {
  addWatch,
  getSignals,
  getWatchlist,
  promoteWatch,
  type SignalRecord,
  type WatchCounts,
  type WatchItem,
  type WatchSource,
} from '../api'

const router = useRouter()

const watchlist = ref<WatchItem[]>([])
const signals = ref<SignalRecord[]>([])
const counts = ref<WatchCounts>({ auto: 0, manual: 0, total: 0 })
const tab = ref<WatchSource>('auto')
const checked = ref<string[]>([])
const addCode = ref('')
const adding = ref(false)
const promoting = ref(false)
const addError = ref('')
const watchLoading = ref(false)
const watchError = ref('')
const toastMsg = ref('')
let toastTimer: number | undefined
let loaded = false

const programItems = computed(() => watchlist.value.filter((i) => i.source === 'auto'))
const manualItems = computed(() => watchlist.value.filter((i) => i.source === 'manual'))
const visibleItems = computed(() =>
  tab.value === 'auto' ? programItems.value : manualItems.value
)
const allChecked = computed(
  () =>
    programItems.value.length > 0 &&
    checked.value.length === programItems.value.length
)

function toast(msg: string) {
  toastMsg.value = msg
  if (toastTimer) window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => (toastMsg.value = ''), 2600)
}

async function loadWatchlist() {
  watchLoading.value = true
  watchError.value = ''
  try {
    const data = await getWatchlist()
    watchlist.value = data.items
    counts.value = data.counts
    // 勾选项只保留仍然存在于程序自选中的代码
    const autoCodes = new Set(data.items.filter((i) => i.source === 'auto').map((i) => i.code))
    checked.value = checked.value.filter((c) => autoCodes.has(c))
  } catch (e: unknown) {
    // 失败时保留旧数据不清空，给出重试入口
    const detail = (e as { response?: { data?: { detail?: string } } })?.response
      ?.data?.detail
    watchError.value = detail ?? '自选股列表加载失败'
  } finally {
    watchLoading.value = false
  }
}

async function loadSignals() {
  signals.value = await getSignals(undefined, 100)
}

async function add() {
  if (!/^\d{6}$/.test(addCode.value)) {
    addError.value = '请输入 6 位股票代码'
    return
  }
  adding.value = true
  addError.value = ''
  try {
    await addWatch(addCode.value, 'manual')
    addCode.value = ''
    await loadWatchlist()
    tab.value = 'manual'
    toast('已加入手动自选')
  } catch (e: unknown) {
    const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    addError.value = msg ?? '添加失败'
  } finally {
    adding.value = false
  }
}

async function remove(code: string) {
  await http.delete(`/watchlist/${code}`)
  await loadWatchlist()
}

function toggleCheck(code: string) {
  checked.value = checked.value.includes(code)
    ? checked.value.filter((c) => c !== code)
    : [...checked.value, code]
}

function toggleAll() {
  checked.value = allChecked.value ? [] : programItems.value.map((i) => i.code)
}

/** 程序自选 → 手动自选 */
async function promoteSelected() {
  if (checked.value.length === 0 || promoting.value) return
  promoting.value = true
  try {
    const { promoted } = await promoteWatch(checked.value)
    checked.value = []
    await loadWatchlist()
    tab.value = 'manual'
    toast(`已将 ${promoted} 只转入手动自选`)
  } catch {
    toast('转入手动自选失败，请重试')
  } finally {
    promoting.value = false
  }
}

function goDetail(code: string) {
  router.push(`/stock/${code}`)
}

function fmtPct(v: number | null | undefined): string {
  return v == null ? '--' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`
}

function pctClass(v: number | null | undefined): string {
  if (v == null || v === 0) return 'text-content-faint'
  return v > 0 ? 'text-up' : 'text-down'
}

/** 成交额（元 → 亿/万），列表里只给量级，不用全精度 */
function fmtAmount(v: number | null | undefined): string {
  if (v == null) return '--'
  if (v >= 1e8) return `${(v / 1e8).toFixed(2)}亿`
  return `${(v / 1e4).toFixed(0)}万`
}

/** 价格/涨跌用同一套色调，几个版面保持一致 */
function priceTone(v: number | null | undefined): string {
  if (v == null || v === 0) return 'text-content'
  return v > 0 ? 'text-up' : 'text-down'
}

onMounted(async () => {
  loaded = true
  await Promise.all([loadWatchlist(), loadSignals()])
})

// keep-alive 缓存下再次进入时刷新（扫描可能刚写了新的程序自选 / 行情已变化）
onActivated(async () => {
  if (!loaded) return
  await Promise.all([loadWatchlist(), loadSignals()])
})
</script>

<template>
  <div class="grid grid-cols-1 gap-5 xl:grid-cols-2">
    <!-- 自选股 -->
    <section class="glass-card flex h-[calc(100vh-8rem)] flex-col p-5">
      <div class="mb-3 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <Star class="h-4 w-4 text-warn" />
          <h3 class="text-subheading">我的自选股</h3>
        </div>
        <span class="text-[12.5px] text-content-faint">{{ counts.total }} 只</span>
      </div>

      <!-- 来源标签 -->
      <div class="seg mb-3 flex">
        <button
          class="seg-item flex flex-1 items-center justify-center gap-1.5"
          :class="tab === 'auto' ? 'seg-item-active' : ''"
          @click="tab = 'auto'"
        >
          <Sparkles class="h-3.5 w-3.5" />
          程序自选
          <span class="font-num text-[12.5px] opacity-80">{{ counts.auto }}</span>
        </button>
        <button
          class="seg-item flex flex-1 items-center justify-center gap-1.5"
          :class="tab === 'manual' ? 'seg-item-active' : ''"
          @click="tab = 'manual'"
        >
          <Star class="h-3.5 w-3.5" />
          手动自选
          <span class="font-num text-[12.5px] opacity-80">{{ counts.manual }}</span>
        </button>
      </div>

      <!-- 手动自选：添加 -->
      <div v-if="tab === 'manual'" class="mb-3 flex gap-2">
        <input
          v-model="addCode"
          type="text"
          maxlength="6"
          placeholder="输入 6 位代码，如 600519"
          class="min-w-0 flex-1 rounded-lg border border-base-600 bg-base-700 px-3 py-2 font-num text-[14.5px] text-content outline-none transition-colors placeholder:text-content-faint focus:border-primary"
          @keyup.enter="add"
        />
        <button class="btn btn-primary px-4 py-2 text-[14.5px]" :disabled="adding" @click="add">
          <Plus class="h-3.5 w-3.5" />
          添加
        </button>
      </div>
      <p v-if="tab === 'manual' && addError" class="mb-2 text-[12.5px] text-up">{{ addError }}</p>

      <!-- 程序自选：批量转入工具栏 -->
      <div
        v-if="tab === 'auto' && programItems.length > 0"
        class="mb-3 flex items-center justify-between gap-2 rounded-lg border border-base-600/60 bg-base-700/30 px-3 py-2"
      >
        <button
          class="flex items-center gap-1.5 text-[13.5px] text-content-muted transition-colors hover:text-primary-light"
          @click="toggleAll"
        >
          <component :is="allChecked ? CheckSquare : Square" class="h-3.5 w-3.5" />
          {{ allChecked ? '取消全选' : '全选' }}
          <span v-if="checked.length" class="font-num text-[12.5px]">已选 {{ checked.length }}</span>
        </button>
        <button
          class="btn btn-primary-soft btn-sm"
          :disabled="checked.length === 0 || promoting"
          @click="promoteSelected"
        >
          <ArrowRightLeft class="h-3.5 w-3.5" />
          {{ promoting ? '转入中...' : '加入手动自选' }}
        </button>
      </div>

      <!-- 列表 -->
      <div class="min-h-0 flex-1 space-y-2 overflow-y-auto">
        <!-- 加载态 -->
        <div v-if="watchLoading" class="flex flex-col items-center gap-2 py-12 text-content-faint">
          <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary-light border-t-transparent" />
          <p class="text-[13.5px]">自选股加载中...</p>
        </div>

        <!-- 错误态（保留旧数据 + 重试） -->
        <div v-else-if="watchError && watchlist.length === 0" class="flex flex-col items-center gap-3 py-12 text-content-faint">
          <p class="text-[14.5px] text-up">{{ watchError }}</p>
          <button class="btn btn-outline" @click="loadWatchlist">重试</button>
        </div>

        <div
          v-for="item in visibleItems"
          :key="item.code"
          class="group flex cursor-pointer items-center justify-between gap-3 rounded border border-base-600/50 bg-base-700/30 px-3 py-2 transition-all hover:border-primary/40 hover:bg-primary/5"
          :class="tab === 'auto' && checked.includes(item.code) ? 'border-primary/50 bg-primary/10' : ''"
          @click="goDetail(item.code)"
        >
          <div class="flex min-w-0 items-center gap-2.5">
            <input
              v-if="tab === 'auto'"
              type="checkbox"
              class="h-3.5 w-3.5 shrink-0 accent-primary"
              :checked="checked.includes(item.code)"
              @click.stop="toggleCheck(item.code)"
            />
            <div class="min-w-0">
              <div class="flex items-center gap-1.5">
                <span class="truncate text-[14.5px] font-medium text-content">{{ item.name }}</span>
                <!-- 来源备注只在手动自选展示：程序自选页签内整列都是程序来源 -->
                <span
                  v-if="item.note && tab === 'manual'"
                  class="shrink-0 rounded bg-base-700 px-1.5 py-0.5 text-[11.5px] text-content-faint"
                  >{{ item.note }}</span
                >
              </div>
              <div class="font-num flex items-center gap-2 text-[12.5px] text-content-faint">
                <span>{{ item.code }}</span>
                <span v-if="item.amount != null">成交 {{ fmtAmount(item.amount) }}</span>
                <span v-if="item.turnover_rate != null">换手 {{ item.turnover_rate.toFixed(1) }}%</span>
              </div>
            </div>
          </div>
          <div class="flex shrink-0 items-center gap-3">
            <div class="text-right">
              <div class="font-num text-[16.5px] font-semibold" :class="priceTone(item.pct_change)">
                {{ item.price?.toFixed(2) ?? '--' }}
              </div>
              <div class="font-num text-[12.5px]" :class="pctClass(item.pct_change)">
                {{ fmtPct(item.pct_change) }}
              </div>
            </div>
            <button
              class="rounded p-1.5 text-content-faint opacity-0 transition-all hover:bg-down/15 hover:text-down group-hover:opacity-100"
              @click.stop="remove(item.code)"
            >
              <Trash2 class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <div v-if="!watchLoading && visibleItems.length === 0" class="flex flex-col items-center gap-2 py-12 text-content-faint">
          <component :is="tab === 'auto' ? Sparkles : Star" class="h-8 w-8 opacity-30" />
          <p v-if="tab === 'auto'" class="text-[14.5px]">暂无程序自选，去选股中心跑一轮扫描</p>
          <p v-else class="text-[14.5px]">暂无手动自选，输入代码添加或从程序自选转入</p>
        </div>
      </div>
    </section>

    <!-- 历史信号 -->
    <section class="glass-card flex h-[calc(100vh-8rem)] flex-col p-5">
      <div class="mb-4 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <Bell class="h-4 w-4 text-primary-light" />
          <h3 class="text-subheading">历史信号流水</h3>
        </div>
        <span class="text-[12.5px] text-content-faint">最近 {{ signals.length }} 条</span>
      </div>

      <div class="min-h-0 flex-1 space-y-2 overflow-y-auto">
        <div
          v-for="sig in signals"
          :key="sig.id"
          class="rounded-lg border border-base-600/50 bg-base-700/30 p-3 transition-colors hover:border-primary/40"
        >
          <div class="flex items-center justify-between">
            <div class="flex cursor-pointer items-center gap-2" @click="goDetail(sig.code)">
              <span class="text-[14.5px] font-medium text-content">{{ sig.name }}</span>
              <span class="font-num text-[12.5px] text-content-faint">{{ sig.code }}</span>
              <span class="rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 text-[12.5px] text-primary-light">
                {{ sig.strategy }}
              </span>
            </div>
            <span class="font-num text-[12.5px] text-content-faint">{{ sig.scan_date }}</span>
          </div>
          <div class="mt-2 flex items-center justify-between">
            <span class="text-[12.5px] leading-relaxed text-content-muted">{{ sig.reason }}</span>
            <span
              class="font-num shrink-0 text-[13.5px] text-content"
              :title="`信号触发价 ${sig.price.toFixed(2)}`"
            >
              <!-- 现价（后端按实时快照附带），无快照时显示触发价 -->
              <span v-if="sig.realtime" :class="pctClass(sig.realtime.pct_change)">
                {{ sig.realtime.price.toFixed(2) }} {{ fmtPct(sig.realtime.pct_change) }}
              </span>
              <span v-else>{{ sig.price.toFixed(2) }}</span>
              <span class="ml-2 text-down">止损 {{ sig.stop_loss?.toFixed(2) ?? '--' }}</span>
              <span class="ml-2 text-up">目标 {{ sig.target_price?.toFixed(2) ?? '--' }}</span>
            </span>
          </div>
        </div>

        <div v-if="signals.length === 0" class="flex flex-col items-center gap-2 py-12 text-content-faint">
          <Bell class="h-8 w-8 opacity-30" />
          <p class="text-[14.5px]">暂无信号，去选股中心跑一轮扫描</p>
        </div>
      </div>
    </section>

    <!-- 轻量 toast -->
    <div
      v-if="toastMsg"
      class="fixed bottom-6 left-1/2 z-50 -translate-x-1/2 rounded-lg border border-primary/40 bg-base-800/95 px-4 py-2 text-[14.5px] text-content shadow-card backdrop-blur"
    >
      {{ toastMsg }}
    </div>
  </div>
</template>
