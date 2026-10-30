<script setup lang="ts">
/**
 * 策略面板：分类分组的策略多选 + 今日命中徽标 + 扫描范围选择。
 * 支持收起为窄条（w-72 → w-11），把宽度让给右侧扫描结果区。
 */
import { computed } from 'vue'
import { ChevronsLeft, ChevronsRight, ListChecks, Pencil } from 'lucide-vue-next'

import type { ScanScope, StrategyInfo } from '../api'

const props = defineProps<{
  strategies: StrategyInfo[]
  selected: string[]
  todayHits: Record<string, number>
  scopes: ScanScope[]
  scope: string
  running: boolean
  /** 是否收起为窄条 */
  collapsed: boolean
}>()

const emit = defineEmits<{
  toggle: [string]
  selectAll: []
  clearAll: []
  edit: [StrategyInfo]
  'update:scope': [string]
  'update:collapsed': [boolean]
}>()

const grouped = computed(() => {
  const map: Record<string, StrategyInfo[]> = {}
  for (const s of props.strategies) {
    ;(map[s.category] ??= []).push(s)
  }
  return map
})

const allSelected = computed(
  () => props.strategies.length > 0 && props.selected.length === props.strategies.length
)
</script>

<template>
  <aside
    class="glass-card flex shrink-0 flex-col overflow-hidden transition-[width,padding] duration-200 ease-out"
    :class="collapsed ? 'w-11 items-center gap-3 py-4' : 'w-72 p-4'"
  >
    <!-- 收起态：窄条，保留展开入口 + 已选数量 + 竖向标题 -->
    <template v-if="collapsed">
      <button
        class="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg border border-base-600 text-content-muted transition-colors hover:border-primary/50 hover:text-primary-light"
        title="展开策略面板"
        @click="emit('update:collapsed', false)"
      >
        <ChevronsRight class="h-4 w-4" />
      </button>
      <span
        class="font-num shrink-0 rounded bg-primary/15 px-1 py-0.5 text-[10px] font-semibold text-primary-light"
        :title="`已选 ${selected.length}/${strategies.length} 个策略`"
      >
        {{ selected.length }}
      </span>
      <div class="flex min-h-0 flex-1 items-center justify-center">
        <span
          class="select-none whitespace-nowrap text-[11px] tracking-wide text-content-faint"
          style="writing-mode: vertical-rl"
        >
          选股策略
        </span>
      </div>
    </template>

    <!-- 展开态 -->
    <template v-else>
      <!-- 标题 + 计数 + 收起 -->
      <div class="mb-3 flex items-center justify-between gap-1">
        <div class="flex min-w-0 items-center gap-2">
          <ListChecks class="h-4 w-4 shrink-0 text-primary-light" />
          <h3 class="truncate text-subheading">选股策略</h3>
        </div>
        <div class="flex shrink-0 items-center gap-1.5">
          <span class="text-[11px] text-content-faint">{{ selected.length }}/{{ strategies.length }}</span>
          <button
            class="flex h-6 w-6 items-center justify-center rounded-md text-content-faint transition-colors hover:bg-base-700/60 hover:text-primary-light"
            title="收起策略面板"
            @click="emit('update:collapsed', true)"
          >
            <ChevronsLeft class="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <!-- 全选 / 清空 -->
      <div class="mb-3 flex overflow-hidden rounded-lg border border-base-600">
        <button
          class="flex-1 border-r border-base-600 px-2 py-1.5 text-[11px] text-content-muted transition-colors last:border-r-0 hover:bg-primary/10 hover:text-primary-light disabled:cursor-not-allowed disabled:opacity-40"
          :disabled="running || allSelected"
          @click="emit('selectAll')"
        >
          全选
        </button>
        <button
          class="flex-1 px-2 py-1.5 text-[11px] text-content-muted transition-colors hover:bg-primary/10 hover:text-primary-light disabled:cursor-not-allowed disabled:opacity-40"
          :disabled="running || selected.length === 0"
          @click="emit('clearAll')"
        >
          清空
        </button>
      </div>

      <!-- 扫描范围 -->
      <label class="mb-4 flex flex-col gap-1.5">
        <span class="text-[11px] text-content-muted">扫描范围</span>
        <select
          :value="scope"
          class="rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none transition-colors focus:border-primary disabled:opacity-50"
          :disabled="running"
          @change="emit('update:scope', ($event.target as HTMLSelectElement).value)"
        >
          <option v-for="s in scopes" :key="s.key" :value="s.key">{{ s.label }}</option>
        </select>
      </label>

      <!-- 策略列表（按分类分组） -->
      <div class="min-h-0 flex-1 space-y-4 overflow-y-auto">
        <div v-for="(list, category) in grouped" :key="category">
          <div class="mb-2 flex items-center gap-2 text-[11px] font-medium tracking-wide text-content-faint">
            <span class="h-1 w-1 rounded-full bg-primary-light" />
            {{ category }}
          </div>
          <div class="space-y-2">
            <div
              v-for="s in list"
              :key="s.name"
              class="group relative flex cursor-pointer items-start gap-2.5 rounded-lg border p-3 transition-all"
              :class="
                selected.includes(s.name)
                  ? 'border-primary/50 bg-primary/10 shadow-[inset_2px_0_0_0_theme(colors.primary.DEFAULT)]'
                  : 'border-base-600/60 hover:border-base-600 hover:bg-base-700/40'
              "
            >
              <input
                type="checkbox"
                class="mt-0.5 h-3.5 w-3.5 accent-primary"
                :checked="selected.includes(s.name)"
                @change="emit('toggle', s.name)"
              />
              <div class="min-w-0 flex-1">
                <div class="flex items-center justify-between gap-2">
                  <span class="text-[13px] font-medium text-content">{{ s.name }}</span>
                  <span class="flex shrink-0 items-center gap-1">
                    <span
                      v-if="s.custom"
                      class="rounded bg-accent/20 px-1.5 py-0.5 text-[10px] text-accent"
                      title="自定义策略"
                      >自定义</span
                    >
                    <span
                      v-if="todayHits[s.name]"
                      class="rounded-full bg-up/15 px-1.5 py-0.5 font-num text-[10px] font-semibold text-up"
                      :title="`今日已命中 ${todayHits[s.name]} 只`"
                    >
                      {{ todayHits[s.name] }}
                    </span>
                    <button
                      class="rounded p-0.5 text-content-faint opacity-0 transition-all hover:bg-base-600 hover:text-primary-light group-hover:opacity-100"
                      title="编辑参数"
                      @click.stop="emit('edit', s)"
                    >
                      <Pencil class="h-3 w-3" />
                    </button>
                  </span>
                </div>
                <div class="mt-0.5 text-[11px] leading-relaxed text-content-faint">
                  {{ s.description }}
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </template>
  </aside>
</template>
