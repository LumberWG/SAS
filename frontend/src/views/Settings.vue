<script setup lang="ts">
/**
 * 系统设置（只读总览）：回答「现在到底按什么规则在跑」。
 *
 * 参数散在四个地方——.env/config.py、app_state 运行时覆盖、页面局部门开关、
 * 硬编码常量（黑名单/阈值/费率）。这里把它们拉成一张表，每项标：
 *   当前值 / 来源（app_state > env > 默认 / 代码）/ 改了要不要重启。
 *
 * 刻意**不做写操作**：78 项里大多数改了必须重启，做成可点开关只会制造
 * 「我点了为什么没变」。真正动态的开关（大盘股过滤、模拟盘模式）在各业务页面。
 */
import { computed, onMounted, ref } from 'vue'
import { RefreshCw, Search } from 'lucide-vue-next'

import { getSettingsOverview, type ParamItem, type SettingsOverview } from '../api'

const data = ref<SettingsOverview | null>(null)
const loading = ref(false)
const error = ref('')
const keyword = ref('')
const onlyRuntime = ref(false)
const onlyNonDefault = ref(false)

const sourceMeta: Record<string, { text: string; cls: string }> = {
  app_state: { text: '运行时', cls: 'border-primary/50 bg-primary/15 text-primary-light' },
  env: { text: '.env', cls: 'border-warn/40 bg-warn/10 text-warn' },
  default: { text: '默认', cls: 'border-base-600 bg-base-700 text-content-faint' },
  code: { text: '硬编码', cls: 'border-base-600 bg-base-700 text-content-muted' },
}

function fmtValue(it: ParamItem): string {
  if (it.type === 'bool') return it.value ? '开' : '关'
  if (it.value === '' || it.value === null || it.value === undefined) return '（空）'
  if (Array.isArray(it.value)) return it.value.join(', ')
  return String(it.value)
}

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  const groups = (data.value?.groups ?? []).map((g) => ({
    name: g.name,
    items: g.items.filter((it) => {
      if (onlyRuntime.value && !it.runtime) return false
      if (onlyNonDefault.value && it.source === 'default') return false
      if (!kw) return true
      return (
        it.key.toLowerCase().includes(kw) ||
        it.label.toLowerCase().includes(kw) ||
        fmtValue(it).toLowerCase().includes(kw)
      )
    }),
  }))
  return groups.filter((g) => g.items.length > 0)
})

const shownCount = computed(() => filtered.value.reduce((n, g) => n + g.items.length, 0))

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await getSettingsOverview()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '加载失败（后端可能未重启）'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-3">
    <!-- 概览条 -->
    <div class="glass-card flex flex-wrap items-center gap-4 px-4 py-3">
      <div>
        <div class="text-[13px] font-semibold text-content">当前生效配置</div>
        <p class="mt-0.5 max-w-[640px] text-[11px] leading-relaxed text-content-faint">
          {{ data?.hint ?? '加载中…' }}
        </p>
      </div>
      <div class="ml-auto flex items-center gap-2">
        <div class="rounded-lg border border-base-600 px-3 py-1 text-center">
          <div class="font-num text-[16px] font-semibold leading-none text-content">
            {{ data?.total ?? '--' }}
          </div>
          <div class="text-[10px] text-content-faint">参数总数</div>
        </div>
        <div class="rounded-lg border border-primary/40 bg-primary/10 px-3 py-1 text-center">
          <div class="font-num text-[16px] font-semibold leading-none text-primary-light">
            {{ data?.runtime_overrides ?? 0 }}
          </div>
          <div class="text-[10px] text-content-faint">运行时覆盖</div>
        </div>
        <button
          class="flex items-center gap-1 rounded border border-base-600 bg-base-700 px-2 py-1 text-[11px] text-content-muted transition-colors hover:border-primary/60 hover:text-primary"
          :disabled="loading"
          @click="load"
        >
          <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />刷新
        </button>
      </div>
    </div>

    <!-- 过滤 -->
    <div class="glass-card flex flex-wrap items-center gap-3 px-4 py-2">
      <label class="flex items-center gap-1.5 rounded border border-base-600 bg-base-700 px-2 py-1">
        <Search class="h-3.5 w-3.5 text-content-faint" />
        <input
          v-model="keyword"
          placeholder="搜参数名 / key / 值"
          class="w-[200px] bg-transparent text-[12px] text-content outline-none placeholder:text-content-faint"
        />
      </label>
      <button
        class="rounded border px-2 py-1 text-[11px] transition-colors"
        :class="onlyRuntime ? 'border-primary/60 bg-primary/15 text-primary-light' : 'border-base-600 bg-base-700 text-content-muted'"
        @click="onlyRuntime = !onlyRuntime"
      >
        只看运行时
      </button>
      <button
        class="rounded border px-2 py-1 text-[11px] transition-colors"
        :class="onlyNonDefault ? 'border-primary/60 bg-primary/15 text-primary-light' : 'border-base-600 bg-base-700 text-content-muted'"
        @click="onlyNonDefault = !onlyNonDefault"
      >
        只看非默认
      </button>
      <span class="text-[11px] text-content-faint">显示 {{ shownCount }} 项</span>
      <span v-if="error" class="text-[11px] text-up">{{ error }}</span>
    </div>

    <!-- 分组表 -->
    <div class="grid grid-cols-1 gap-3 xl:grid-cols-2">
      <section v-for="g in filtered" :key="g.name" class="glass-card px-4 py-3">
        <div class="mb-1.5 flex items-center justify-between">
          <h3 class="text-[13px] font-semibold text-content">{{ g.name }}</h3>
          <span class="font-num text-[10px] text-content-faint">{{ g.items.length }} 项</span>
        </div>
        <div class="flex flex-col">
          <div
            v-for="it in g.items"
            :key="it.key"
            class="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] items-center gap-2 border-t border-base-600/30 py-1 first:border-t-0"
            :title="`${it.key}${it.note ? ' · ' + it.note : ''}`"
          >
            <div class="min-w-0">
              <div class="truncate text-[11px] text-content">{{ it.label }}</div>
              <div class="font-num truncate text-[10px] text-content-faint">{{ it.key }}</div>
            </div>
            <div class="font-num max-w-[180px] truncate text-right text-[12px] text-content">
              {{ fmtValue(it) }}
            </div>
            <span
              class="rounded border px-1.5 py-0.5 text-[10px]"
              :class="sourceMeta[it.source]?.cls ?? 'border-base-600 text-content-faint'"
            >
              {{ sourceMeta[it.source]?.text ?? it.source }}
            </span>
            <span
              class="rounded border px-1.5 py-0.5 text-[10px]"
              :class="
                it.restart_required
                  ? 'border-base-600 bg-base-700 text-content-faint'
                  : 'border-primary/40 bg-primary/10 text-primary-light'
              "
            >
              {{ it.restart_required ? '需重启' : '即时' }}
            </span>
          </div>
        </div>
      </section>
    </div>

    <p v-if="!loading && filtered.length === 0" class="py-8 text-center text-[12px] text-content-faint">
      没有匹配的参数
    </p>
  </div>
</template>
