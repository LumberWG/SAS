<script setup lang="ts">
/**
 * 扫描空状态：本轮无命中（含历史命中上下文）/ 扫描中断 / 未开始。
 */
import { Square } from 'lucide-vue-next'

defineProps<{
  status: 'pending' | 'running' | 'done' | 'error' | null
  todayHitTotal: number
}>()
</script>

<template>
  <div
    class="glass-card flex min-h-0 flex-1 flex-col items-center justify-center gap-4 p-8 text-content-faint"
  >
    <div class="relative">
      <div class="absolute inset-0 rounded-full bg-primary/20 blur-xl" />
      <div
        class="relative flex h-16 w-16 items-center justify-center rounded-full border border-base-600 bg-base-700/60"
      >
        <Square class="h-7 w-7 text-content-muted" />
      </div>
    </div>

    <p class="text-[15px] font-medium text-content-muted">
      {{
        status === 'done'
          ? '本轮扫描未命中'
          : status === 'error'
            ? '上一轮扫描被中断（可能后端重启），请重新开始扫描'
            : '选择策略与范围后点击「开始扫描」'
      }}
    </p>

    <!-- 本轮无命中时给出上下文：今日历史命中 + 操作建议 -->
    <template v-if="status === 'done'">
      <p v-if="todayHitTotal > 0" class="text-[12px] text-content-muted">
        今日历史命中共
        <span class="font-num font-semibold text-primary-light">{{ todayHitTotal }}</span>
        个，
        <router-link
          to="/signals"
          class="text-primary-light underline underline-offset-2"
        >
          前往「信号与自选」查看
        </router-link>
      </p>
      <div class="flex flex-wrap items-center justify-center gap-2 text-[11px]">
        <span class="rounded-full border border-base-600 px-2.5 py-1">点击策略 ✏️ 放宽参数</span>
        <span class="rounded-full border border-base-600 px-2.5 py-1">扩大扫描范围</span>
        <span class="rounded-full border border-base-600 px-2.5 py-1">更换策略组合</span>
      </div>
    </template>
    <p v-else class="max-w-md text-center text-[11px] leading-relaxed">
      冷缓存全市场扫描约需数分钟；选择「成交额前 500」范围可快速获得结果。命中信号自动存入信号库
    </p>
  </div>
</template>
