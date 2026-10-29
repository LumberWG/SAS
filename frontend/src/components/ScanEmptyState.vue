<script setup lang="ts">
/**
 * 扫描空状态：本轮无命中（含历史命中上下文）/ 扫描中断 / 未开始。
 * 未开始时用「一条环境指标 + 三步流程」填充留白，保持与页面一致的简洁密度。
 */
import { computed } from 'vue'
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Loader2,
  SlidersHorizontal,
} from 'lucide-vue-next'

const props = defineProps<{
  status: 'pending' | 'running' | 'done' | 'error' | null
  todayHitTotal: number
  /** 左侧已选策略数 */
  strategyCount: number
  /** 当前扫描范围显示名 */
  scopeLabel: string
  /** 本地K线缓存覆盖情况 */
  klineCached: number
  klineTotal: number
  klineRatio: number
}>()

const klineReady = computed(() => props.klineTotal > 0 && props.klineRatio >= 99)

const klineText = computed(() =>
  props.klineTotal > 0 ? `${props.klineCached}/${props.klineTotal}` : '暂无数据'
)

const heading = computed(() => {
  if (props.status === 'done') return '本轮扫描未命中'
  if (props.status === 'error') return '上一轮扫描被中断（可能后端重启），请重新开始扫描'
  if (props.status === 'running' || props.status === 'pending') return '扫描进行中，命中结果会自动出现在这里'
  return '选择策略与范围后点击「开始扫描」'
})

const icon = computed(() => {
  if (props.status === 'done') return CheckCircle2
  if (props.status === 'error') return AlertCircle
  if (props.status === 'running' || props.status === 'pending') return Loader2
  return SlidersHorizontal
})

const steps = ['勾选策略', '选择范围', '开始扫描']
</script>

<template>
  <div
    class="glass-card flex min-h-0 flex-1 flex-col items-center justify-center gap-7 overflow-y-auto p-8 text-content-faint"
  >
    <!-- 图标 + 标题 -->
    <div class="flex flex-col items-center gap-4 text-center">
      <div class="relative">
        <div class="absolute inset-0 rounded-full bg-primary/20 blur-xl" />
        <div
          class="relative flex h-16 w-16 items-center justify-center rounded-full border border-base-600 bg-base-700/60"
        >
          <component
            :is="icon"
            class="h-7 w-7"
            :class="[
              status === 'done'
                ? 'text-up'
                : status === 'error'
                  ? 'text-down'
                  : 'text-content-muted',
              status === 'running' || status === 'pending' ? 'animate-spin' : '',
            ]"
          />
        </div>
      </div>

      <p class="max-w-lg text-[15px] font-medium text-content-muted">{{ heading }}</p>
    </div>

    <!-- 本轮无命中：历史命中上下文 + 放宽建议 -->
    <template v-if="status === 'done'">
      <p v-if="todayHitTotal > 0" class="text-[12px] text-content-muted">
        今日历史命中共
        <span class="font-num font-semibold text-primary-light">{{ todayHitTotal }}</span>
        个，
        <router-link to="/signals" class="text-primary-light underline underline-offset-2">
          前往「② 二选 · 自选股」查看
        </router-link>
      </p>
      <div class="flex flex-wrap items-center justify-center gap-2 text-[11px]">
        <span class="rounded-full border border-base-600 px-2.5 py-1">点击策略 ✏️ 放宽参数</span>
        <span class="rounded-full border border-base-600 px-2.5 py-1">扩大扫描范围</span>
        <span class="rounded-full border border-base-600 px-2.5 py-1">更换策略组合</span>
      </div>
    </template>

    <!-- 未开始 / 扫描中：一条横向指标 + 三步流程 -->
    <template v-else>
      <div class="w-full max-w-3xl">
        <div
          class="flex flex-col divide-y divide-base-600/60 overflow-hidden rounded-xl border border-base-600/60 bg-base-700/20 sm:flex-row sm:divide-x sm:divide-y-0"
        >
          <div class="flex-1 px-5 py-4">
            <div class="text-[11px] text-content-faint">今日历史命中</div>
            <div class="mt-2 flex items-baseline gap-1.5">
              <span class="font-num text-[22px] font-semibold leading-none text-primary-light">
                {{ todayHitTotal }}
              </span>
              <span class="text-[11px] text-content-faint">个信号</span>
            </div>
          </div>

          <div class="flex-1 px-5 py-4">
            <div class="flex items-center gap-1.5 text-[11px] text-content-faint">
              本地K线覆盖
              <span v-if="klineReady" class="rounded bg-up/15 px-1.5 py-0.5 text-up">已就绪</span>
            </div>
            <div class="mt-2 flex items-baseline gap-1.5">
              <span
                class="font-num text-[22px] font-semibold leading-none"
                :class="klineReady ? 'text-up' : 'text-content'"
              >
                {{ klineTotal > 0 ? `${klineRatio}%` : '—' }}
              </span>
              <span class="font-num text-[11px] text-content-faint">{{ klineText }}</span>
            </div>
          </div>

          <div class="flex-1 px-5 py-4">
            <div class="text-[11px] text-content-faint">当前选择</div>
            <div class="mt-2 flex items-baseline gap-1.5">
              <span class="font-num text-[22px] font-semibold leading-none text-content">
                {{ strategyCount }}
              </span>
              <span class="truncate text-[12px] text-content-muted" :title="scopeLabel">
                策略 · {{ scopeLabel }}
              </span>
            </div>
          </div>
        </div>
      </div>

      <!-- 三步流程（仅未开始，扫描中显示无意义） -->
      <div
        v-if="status !== 'running' && status !== 'pending'"
        class="flex flex-wrap items-center justify-center gap-2 text-[12px]"
      >
        <template v-for="(step, i) in steps" :key="step">
          <div class="flex items-center gap-1.5 rounded-full border border-base-600/60 px-3 py-1.5">
            <span class="font-num text-primary-light">{{ i + 1 }}</span>
            <span>{{ step }}</span>
          </div>
          <ChevronRight v-if="i < steps.length - 1" class="h-3.5 w-3.5 opacity-40" />
        </template>
      </div>
    </template>
  </div>
</template>
