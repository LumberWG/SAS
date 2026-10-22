<script setup lang="ts">
/**
 * 信号与自选股：自选股管理（实时价）+ 历史信号流水。
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Bell, Plus, Star, Trash2, X } from 'lucide-vue-next'

import http, {
  getSignals,
  type SignalRecord,
  type WatchItem,
} from '../api'

const router = useRouter()

const watchlist = ref<WatchItem[]>([])
const signals = ref<SignalRecord[]>([])
const addCode = ref('')
const adding = ref(false)
const addError = ref('')
const watchLoading = ref(false)
const watchError = ref('')

async function loadWatchlist() {
  watchLoading.value = true
  watchError.value = ''
  try {
    const data = await http.get('/watchlist').then((r) => r.data)
    watchlist.value = data.items
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
    await http.post(`/watchlist/${addCode.value}`)
    addCode.value = ''
    await loadWatchlist()
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

function goDetail(code: string) {
  router.push(`/stock/${code}`)
}

onMounted(async () => {
  await Promise.all([loadWatchlist(), loadSignals()])
})
</script>

<template>
  <div class="grid grid-cols-1 gap-5 xl:grid-cols-2">
    <!-- 自选股 -->
    <section class="glass-card flex h-[calc(100vh-8rem)] flex-col p-5">
      <div class="mb-4 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <Star class="h-4 w-4 text-warn" />
          <h3 class="text-subheading">我的自选股</h3>
        </div>
        <span class="text-[11px] text-content-faint">{{ watchlist.length }} 只</span>
      </div>

      <!-- 添加 -->
      <div class="mb-4 flex gap-2">
        <input
          v-model="addCode"
          type="text"
          maxlength="6"
          placeholder="输入 6 位代码，如 600519"
          class="min-w-0 flex-1 rounded-lg border border-base-600 bg-base-700 px-3 py-2 font-num text-[13px] text-content outline-none transition-colors placeholder:text-content-faint focus:border-primary"
          @keyup.enter="add"
        />
        <button
          class="flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-[13px] font-medium text-white transition-all hover:brightness-110 disabled:opacity-50"
          :disabled="adding"
          @click="add"
        >
          <Plus class="h-3.5 w-3.5" />
          添加
        </button>
      </div>
      <p v-if="addError" class="mb-2 text-[11px] text-up">{{ addError }}</p>

      <!-- 列表 -->
      <div class="min-h-0 flex-1 space-y-2 overflow-y-auto">
        <!-- 加载态 -->
        <div v-if="watchLoading" class="flex flex-col items-center gap-2 py-12 text-content-faint">
          <div class="h-6 w-6 animate-spin rounded-full border-2 border-primary-light border-t-transparent" />
          <p class="text-[12px]">自选股加载中...</p>
        </div>

        <!-- 错误态（保留旧数据 + 重试） -->
        <div v-else-if="watchError && watchlist.length === 0" class="flex flex-col items-center gap-3 py-12 text-content-faint">
          <p class="text-[13px] text-up">{{ watchError }}</p>
          <button
            class="rounded-lg border border-base-600 px-3 py-1.5 text-[12px] text-content-muted transition-colors hover:border-primary/50 hover:text-primary-light"
            @click="loadWatchlist"
          >
            重试
          </button>
        </div>

        <div
          v-for="item in watchlist"
          :key="item.code"
          class="group flex cursor-pointer items-center justify-between rounded-lg border border-base-600/50 bg-base-700/30 p-3 transition-all hover:border-primary/40 hover:bg-primary/5"
          @click="goDetail(item.code)"
        >
          <div class="flex items-center gap-3">
            <div>
              <div class="text-[13px] font-medium text-content">{{ item.name }}</div>
              <div class="font-num text-[11px] text-content-faint">{{ item.code }}</div>
            </div>
          </div>
          <div class="flex items-center gap-4">
            <div class="text-right">
              <div class="font-num text-[15px] font-semibold" :class="item.pct_change != null && item.pct_change !== 0 ? (item.pct_change > 0 ? 'text-up' : 'text-down') : 'text-content'">
                {{ item.price?.toFixed(2) ?? '--' }}
              </div>
              <div class="font-num text-[11px]" :class="item.pct_change != null && item.pct_change !== 0 ? (item.pct_change > 0 ? 'text-up' : 'text-down') : 'text-content-faint'">
                {{ item.pct_change != null ? (item.pct_change > 0 ? '+' : '') + item.pct_change.toFixed(2) + '%' : '--' }}
              </div>
            </div>
            <button
              class="rounded-lg p-1.5 text-content-faint opacity-0 transition-all hover:bg-down/15 hover:text-down group-hover:opacity-100"
              @click.stop="remove(item.code)"
            >
              <Trash2 class="h-3.5 w-3.5" />
            </button>
          </div>
        </div>

        <div v-if="watchlist.length === 0" class="flex flex-col items-center gap-2 py-12 text-content-faint">
          <Star class="h-8 w-8 opacity-30" />
          <p class="text-[13px]">暂无自选股，输入代码添加</p>
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
        <span class="text-[11px] text-content-faint">最近 {{ signals.length }} 条</span>
      </div>

      <div class="min-h-0 flex-1 space-y-2 overflow-y-auto">
        <div
          v-for="sig in signals"
          :key="sig.id"
          class="rounded-lg border border-base-600/50 bg-base-700/30 p-3 transition-colors hover:border-primary/40"
        >
          <div class="flex items-center justify-between">
            <div class="flex cursor-pointer items-center gap-2" @click="goDetail(sig.code)">
              <span class="text-[13px] font-medium text-content">{{ sig.name }}</span>
              <span class="font-num text-[11px] text-content-faint">{{ sig.code }}</span>
              <span class="rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-primary-light">
                {{ sig.strategy }}
              </span>
            </div>
            <span class="font-num text-[11px] text-content-faint">{{ sig.scan_date }}</span>
          </div>
          <div class="mt-2 flex items-center justify-between">
            <span class="text-[11px] leading-relaxed text-content-muted">{{ sig.reason }}</span>
            <span class="font-num shrink-0 text-[12px] text-content">
              {{ sig.price.toFixed(2) }}
              <span class="ml-2 text-down">止损 {{ sig.stop_loss?.toFixed(2) ?? '--' }}</span>
              <span class="ml-2 text-up">目标 {{ sig.target_price?.toFixed(2) ?? '--' }}</span>
            </span>
          </div>
        </div>

        <div v-if="signals.length === 0" class="flex flex-col items-center gap-2 py-12 text-content-faint">
          <Bell class="h-8 w-8 opacity-30" />
          <p class="text-[13px]">暂无信号，去选股中心跑一轮扫描</p>
        </div>
      </div>
    </section>
  </div>
</template>
