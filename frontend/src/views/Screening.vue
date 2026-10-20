<script setup lang="ts">
/**
 * 选股中心：策略多选 → 触发扫描 → 进度轮询 → 命中结果表。
 */
import { computed, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Play, Search, Square } from 'lucide-vue-next'

import {
  getScanTask,
  getStrategies,
  startScan,
  type ScanHit,
  type ScanTask,
  type StrategyInfo,
} from '../api'

const router = useRouter()

const strategies = ref<StrategyInfo[]>([])
const selected = ref<string[]>([])
const task = ref<ScanTask | null>(null)
let poller: number | undefined

const grouped = computed(() => {
  const map: Record<string, StrategyInfo[]> = {}
  for (const s of strategies.value) {
    ;(map[s.category] ??= []).push(s)
  }
  return map
})

const running = computed(() => task.value?.status === 'running' || task.value?.status === 'pending')

async function loadStrategies() {
  strategies.value = await getStrategies()
  // 默认全选
  selected.value = strategies.value.map((s) => s.name)
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
  const { task_id } = await startScan(selected.value)
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

function fmtPrice(v: number): string {
  return v.toFixed(2)
}
</script>

<template>
  <div class="flex h-[calc(100vh-8rem)] gap-5">
    <!-- 左侧策略面板 -->
    <aside class="glass-card w-72 shrink-0 overflow-y-auto p-4">
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-subheading">选股策略</h3>
        <span class="text-[11px] text-content-faint">{{ selected.length }}/{{ strategies.length }} 已选</span>
      </div>

      <div v-for="(list, category) in grouped" :key="category" class="mb-4">
        <div class="mb-2 text-[11px] font-medium tracking-wide text-content-faint">{{ category }}</div>
        <div class="space-y-2">
          <label
            v-for="s in list"
            :key="s.name"
            class="flex cursor-pointer items-start gap-2.5 rounded-lg border p-2.5 transition-all"
            :class="
              selected.includes(s.name)
                ? 'border-primary/50 bg-primary/10'
                : 'border-base-600/60 hover:border-base-600 hover:bg-base-700/40'
            "
          >
            <input
              type="checkbox"
              class="mt-0.5 h-3.5 w-3.5 accent-[#2563EB]"
              :checked="selected.includes(s.name)"
              @change="toggleStrategy(s.name)"
            />
            <div class="min-w-0">
              <div class="text-[13px] font-medium text-content">{{ s.name }}</div>
              <div class="mt-0.5 text-[11px] leading-relaxed text-content-faint">{{ s.description }}</div>
            </div>
          </label>
        </div>
      </div>
    </aside>

    <!-- 右侧扫描区 -->
    <section class="flex min-w-0 flex-1 flex-col gap-4">
      <!-- 操作栏 -->
      <div class="glass-card flex items-center gap-4 p-4">
        <button
          class="flex items-center gap-2 rounded-lg bg-gradient-to-r from-primary to-primary-light px-5 py-2.5 text-[13px] font-semibold text-white shadow-glow transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
          :disabled="running || selected.length === 0"
          @click="start"
        >
          <Play class="h-4 w-4" />
          {{ running ? '扫描中...' : '开始全市场扫描' }}
        </button>
        <div v-if="task" class="min-w-0 flex-1">
          <div class="mb-1.5 flex items-center justify-between text-[12px]">
            <span class="text-content-muted">
              <Search class="mr-1 inline h-3.5 w-3.5" />
              {{ task.status === 'done' ? '扫描完成' : task.status === 'error' ? '扫描失败' : '正在扫描' }}
              <span v-if="task.total"> · {{ task.scanned }}/{{ task.total }} 只</span>
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
        <div v-if="task && task.status === 'done'" class="shrink-0 text-right">
          <div class="font-num text-[20px] font-semibold text-primary-light">{{ task.hit_count }}</div>
          <div class="text-[11px] text-content-faint">命中信号</div>
        </div>
      </div>

      <!-- 结果表 -->
      <div class="glass-card min-h-0 flex-1 overflow-auto">
        <table v-if="task && task.hits.length > 0" class="w-full text-left text-[13px]">
          <thead class="sticky top-0 z-10 bg-base-800/95 text-[11px] text-content-faint backdrop-blur">
            <tr class="border-b border-base-600/60">
              <th class="px-4 py-2.5 font-medium">代码</th>
              <th class="px-4 py-2.5 font-medium">名称</th>
              <th class="px-4 py-2.5 text-right font-medium">最新价</th>
              <th class="px-4 py-2.5 font-medium">命中策略</th>
              <th class="px-4 py-2.5 font-medium">命中原因</th>
              <th class="px-4 py-2.5 text-right font-medium">止损价</th>
              <th class="px-4 py-2.5 text-right font-medium">目标价</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="hit in task.hits"
              :key="hit.code + hit.strategy"
              class="cursor-pointer border-b border-base-600/30 transition-colors hover:bg-primary/10"
              @click="goDetail(hit)"
            >
              <td class="font-num px-4 py-2.5 text-content-muted">{{ hit.code }}</td>
              <td class="px-4 py-2.5 font-medium text-content">{{ hit.name }}</td>
              <td class="font-num px-4 py-2.5 text-right text-content">{{ fmtPrice(hit.price) }}</td>
              <td class="px-4 py-2.5">
                <span class="rounded-md border border-primary/40 bg-primary/10 px-2 py-0.5 text-[11px] text-primary-light">
                  {{ hit.strategy }}
                </span>
              </td>
              <td class="max-w-[280px] truncate px-4 py-2.5 text-content-muted" :title="hit.reason">
                {{ hit.reason }}
              </td>
              <td class="font-num px-4 py-2.5 text-right text-down">{{ hit.stop_loss?.toFixed(2) ?? '--' }}</td>
              <td class="font-num px-4 py-2.5 text-right text-up">{{ hit.target_price?.toFixed(2) ?? '--' }}</td>
            </tr>
          </tbody>
        </table>

        <!-- 空状态 -->
        <div v-else class="flex h-full flex-col items-center justify-center gap-3 text-content-faint">
          <Square class="h-10 w-10 opacity-30" />
          <p class="text-[13px]">
            {{ task?.status === 'done' ? '本轮扫描没有命中任何策略' : '选择策略后点击「开始全市场扫描」' }}
          </p>
          <p class="text-[11px]">全市场扫描约需数分钟（冷缓存），结果自动存入信号库</p>
        </div>
      </div>
    </section>
  </div>
</template>
