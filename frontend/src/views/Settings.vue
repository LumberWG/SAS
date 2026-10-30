<script setup lang="ts">
/**
 * 系统设置：分组 tab + 可编辑。
 *
 * 写模型（后端 `app/param_specs.py`）：改动落 `app_state` 覆盖层，同时就地改
 * `Settings` 单例 → 下一个 tick / 下一轮扫描即生效；重启时由启动流程重放，不会丢。
 *
 * 只读项：路径 / 列表类（`kline_dir`、`db_path`、`cors_origins` 等），它们在进程
 * 启动时就被静态挂载、CORS、缓存目录捕获，运行中改只会造成「显示改了实际没变」。
 */
import { computed, onMounted, ref } from 'vue'
import { Info, RefreshCw, RotateCcw, Save, Search } from 'lucide-vue-next'

import {
  getSettingsOverview,
  resetSettings,
  updateSettings,
  type ParamItem,
  type SettingsOverview,
} from '../api'

const data = ref<SettingsOverview | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const toast = ref('')
const keyword = ref('')
const activeGroup = ref('')
const onlyChanged = ref(false)

/** 编辑草稿：key → 输入值 */
const draft = ref<Record<string, string | number | boolean>>({})
/** 服务端当前生效值快照，用于算 dirty 与「单行还原」 */
const baseline = ref<Record<string, string | number | boolean>>({})

const sourceMeta: Record<string, { text: string; cls: string }> = {
  app_state: { text: '运行时', cls: 'border-primary/50 bg-primary/15 text-primary-light' },
  env: { text: '.env', cls: 'border-warn/40 bg-warn/10 text-warn' },
  default: { text: '默认', cls: 'border-base-600 bg-base-700 text-content-faint' },
  code: { text: '硬编码', cls: 'border-base-600 bg-base-700 text-content-muted' },
}

function hydrate(overview: SettingsOverview) {
  data.value = overview
  const d: Record<string, string | number | boolean> = {}
  const b: Record<string, string | number | boolean> = {}
  for (const g of overview.groups) {
    for (const it of g.items) {
      const v = it.value === null || it.value === undefined ? '' : (it.value as string | number | boolean)
      d[it.key] = v
      b[it.key] = v
    }
  }
  draft.value = d
  baseline.value = b
  if (!activeGroup.value && overview.groups.length) activeGroup.value = overview.groups[0].name
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    hydrate(await getSettingsOverview())
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '加载失败（后端可能未重启）'
  } finally {
    loading.value = false
  }
}

function fmtValue(it: ParamItem): string {
  const v = baseline.value[it.key]
  if (it.kind === 'bool') return v ? '开' : '关'
  if (v === '' || v === null || v === undefined) return '（空）'
  if (Array.isArray(v)) return v.join(', ')
  return String(v)
}

/** 草稿与生效值是否不同（数字按数值比，避免 '10' vs 10 误判） */
function isDirty(it: ParamItem): boolean {
  if (!it.writable) return false
  const a = draft.value[it.key]
  const b = baseline.value[it.key]
  if (it.kind === 'bool') return Boolean(a) !== Boolean(b)
  if (it.kind === 'number') return Number(a) !== Number(b)
  return String(a ?? '') !== String(b ?? '')
}

const dirtyKeys = computed(() => {
  const keys: string[] = []
  for (const g of data.value?.groups ?? []) {
    for (const it of g.items) if (isDirty(it)) keys.push(it.key)
  }
  return keys
})

const groups = computed(() =>
  (data.value?.groups ?? []).map((g) => ({
    name: g.name,
    total: g.items.length,
    changed: g.items.filter((it) => it.source === 'app_state').length,
  })),
)

/** 当前分组里有多少项「改了还没保存」（tab 上的角标是已覆盖数，两者含义不同） */
const activeGroupDirty = computed(
  () =>
    (data.value?.groups ?? [])
      .find((g) => g.name === activeGroup.value)
      ?.items.filter((it) => isDirty(it)).length ?? 0
)

/**
 * 行悬停提示：完整说明（note）、完整 key、当前值与待保存值都放这里。
 *
 * 行内只保留短标签，长说明塞进 tooltip —— 否则一条 `paper_exclude_large_cap` 的备注
 * 就能把卡片撑成三行、整个网格高低不齐。
 */
function tipOf(it: ParamItem): string {
  const lines = [it.label, it.key, `当前值：${fmtValue(it)}`]
  if (isDirty(it)) {
    const v = draft.value[it.key]
    lines.push(`待保存：${typeof v === 'boolean' ? (v ? '开' : '关') : String(v ?? '')}`)
  }
  if (it.note) lines.push(`说明：${it.note}`)
  if (!it.writable) lines.push('路径/列表类参数：仅启动时读取，需改 .env 后重启后端')
  return lines.join('\n')
}

const currentItems = computed(() => {
  const g = (data.value?.groups ?? []).find((x) => x.name === activeGroup.value)
  if (!g) return [] as ParamItem[]
  const kw = keyword.value.trim().toLowerCase()
  return g.items.filter((it) => {
    if (onlyChanged.value && it.source !== 'app_state') return false
    if (!kw) return true
    return (
      it.key.toLowerCase().includes(kw) ||
      it.label.toLowerCase().includes(kw) ||
      fmtValue(it).toLowerCase().includes(kw)
    )
  })
})

async function save() {
  const changes: Record<string, string | number | boolean> = {}
  for (const g of data.value?.groups ?? []) {
    for (const it of g.items) {
      if (isDirty(it)) changes[it.key] = draft.value[it.key]
    }
  }
  if (!Object.keys(changes).length) return
  saving.value = true
  error.value = ''
  try {
    hydrate(await updateSettings(changes))
    toast.value = `已保存 ${Object.keys(changes).length} 项，立即生效`
    window.setTimeout(() => (toast.value = ''), 2500)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '保存失败'
  } finally {
    saving.value = false
  }
}

async function resetKeys(keys: string[]) {
  if (!keys.length) return
  saving.value = true
  error.value = ''
  try {
    hydrate(await resetSettings(keys))
    toast.value = `已还原 ${keys.length} 项为 .env 值`
    window.setTimeout(() => (toast.value = ''), 2500)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '还原失败'
  } finally {
    saving.value = false
  }
}

/** 单行还原：只是把草稿拉回生效值（真正落库走「还原」按钮） */
function revertRow(it: ParamItem) {
  draft.value[it.key] = baseline.value[it.key]
}

/** 放弃全部未保存改动（底部悬浮条用）：草稿整体拉回生效值，不触库 */
function discardAll() {
  draft.value = { ...baseline.value }
}

onMounted(load)
</script>

<template>
  <div class="space-y-3">
    <!-- 概览 + 操作 -->
    <div class="glass-card px-4 py-2.5">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-2">
            <span class="h-1.5 w-1.5 rounded-full bg-primary shadow-glow"></span>
            <h2 class="text-[13px] font-semibold text-content">当前生效配置</h2>
          </div>
          <p class="mt-0.5 max-w-[720px] text-[11px] leading-relaxed text-content-faint">
            {{ data?.hint ?? '加载中…' }}
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <div class="flex items-center gap-1.5 rounded-lg border border-base-600/60 bg-base-700/30 px-2.5 py-1">
            <span class="font-num text-[15px] font-semibold leading-none text-content">{{ data?.total ?? '--' }}</span>
            <span class="text-[10px] text-content-faint">参数</span>
          </div>
          <div class="flex items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-2.5 py-1">
            <span class="font-num text-[15px] font-semibold leading-none text-primary-light">
              {{ data?.runtime_overrides ?? 0 }}
            </span>
            <span class="text-[10px] text-content-faint">已覆盖</span>
          </div>
          <div class="mx-0.5 hidden h-6 w-px bg-base-600/60 sm:block"></div>
          <button
            class="flex items-center gap-1 rounded-lg border border-primary/60 bg-primary/15 px-2.5 py-1 text-[12px] font-medium text-primary-light transition-colors hover:bg-primary/25 disabled:opacity-40"
            :disabled="!dirtyKeys.length || saving"
            :title="dirtyKeys.length ? '写入 app_state 覆盖层并立即生效（重启保留）' : '当前没有改动'"
            @click="save"
          >
            <Save class="h-3.5 w-3.5" />
            保存{{ dirtyKeys.length ? ` (${dirtyKeys.length})` : '' }}
          </button>
          <button
            class="flex items-center gap-1 rounded-lg border border-base-600 bg-base-700 px-2.5 py-1 text-[11px] text-content-muted transition-colors hover:border-down/60 hover:text-down disabled:opacity-40"
            :disabled="saving || !(data?.runtime_overrides ?? 0)"
            title="把所有已覆盖项还原为 .env / 环境变量里的值"
            @click="resetKeys(dirtyKeys.length ? dirtyKeys : [])"
          >
            <RotateCcw class="h-3.5 w-3.5" />还原
          </button>
          <button
            class="flex items-center gap-1 rounded-lg border border-base-600 bg-base-700 px-2 py-1 text-[11px] text-content-muted transition-colors hover:border-primary/60 hover:text-primary"
            :disabled="loading"
            title="重新拉取当前生效值（丢弃未保存草稿）"
            @click="load"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
          </button>
        </div>
      </div>
      <div v-if="toast" class="mt-1.5 rounded bg-down/10 px-2 py-1 text-[11px] text-down">{{ toast }}</div>
      <div v-if="error" class="mt-1.5 rounded bg-up/10 px-2 py-1 text-[11px] text-up">{{ error }}</div>
    </div>

    <!-- 过滤 + 分组 tab + 参数行，收进同一张卡片 -->
    <div class="glass-card p-3">
      <div class="flex flex-wrap items-center gap-2">
        <label
          class="flex items-center gap-1.5 rounded-lg border border-base-600 bg-base-700/60 px-2.5 py-1.5 transition-colors focus-within:border-primary"
        >
          <Search class="h-3.5 w-3.5 shrink-0 text-content-faint" />
          <input
            v-model="keyword"
            placeholder="搜参数名 / key / 值"
            class="w-[170px] bg-transparent text-[12px] text-content outline-none placeholder:text-content-faint sm:w-[210px]"
          />
          <button
            v-if="keyword"
            type="button"
            class="shrink-0 text-[13px] leading-none text-content-faint transition-colors hover:text-content"
            title="清空搜索"
            @click="keyword = ''"
          >
            ×
          </button>
        </label>
        <button
          class="rounded-lg border px-2.5 py-1.5 text-[11px] transition-colors"
          :class="
            onlyChanged
              ? 'border-primary/60 bg-primary/15 text-primary-light'
              : 'border-base-600 bg-base-700 text-content-muted hover:text-content'
          "
          @click="onlyChanged = !onlyChanged"
        >
          只看已覆盖
        </button>
        <span class="text-[11px] text-content-faint">共 {{ currentItems.length }} 项</span>
      </div>

      <!-- 分组 tab：胶囊底槽，角标为已覆盖数 -->
      <div class="mt-2.5 flex flex-wrap items-center gap-1 rounded-lg bg-base-800/40 p-1">
        <button
          v-for="g in groups"
          :key="g.name"
          type="button"
          class="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-[11.5px] font-medium transition-colors"
          :class="
            activeGroup === g.name
              ? 'bg-primary/15 text-primary-light shadow-[inset_0_0_0_1px_rgba(56,189,248,0.25)]'
              : 'text-content-muted hover:bg-base-700/60 hover:text-content'
          "
          @click="activeGroup = g.name"
        >
          {{ g.name }}
          <span class="font-num text-[10px] font-normal text-content-faint">{{ g.total }}</span>
          <span
            v-if="g.changed"
            class="rounded bg-primary/20 px-1 font-num text-[10px] text-primary-light"
            title="该组已覆盖（app_state）的项数"
          >
            {{ g.changed }}
          </span>
        </button>
      </div>

      <!-- 当前组标题 -->
      <div class="mb-1.5 mt-3 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span class="text-[12px] font-medium text-content">{{ activeGroup }}</span>
        <span class="rounded bg-base-600/40 px-1.5 py-0.5 font-num text-[10px] text-content-muted">
          {{ currentItems.length }}
        </span>
        <span
          v-if="activeGroupDirty"
          class="rounded bg-primary/20 px-1.5 py-0.5 text-[10px] text-primary-light"
        >
          本组已改 {{ activeGroupDirty }}
        </span>
        <span class="flex-1"></span>
        <span class="flex items-center gap-1 text-[10px] text-content-faint">
          <Info class="h-3 w-3" />悬停任一行可看完整 key、当前值与说明
        </span>
      </div>

      <!-- 参数行：行内只留短标签与控件，长说明一律收进 tooltip，避免卡片高低不齐 -->
      <div class="grid grid-cols-1 gap-1.5 lg:grid-cols-2 2xl:grid-cols-3">
        <div
          v-for="it in currentItems"
          :key="it.key"
          class="group flex items-start gap-2 rounded-lg border px-2.5 py-2 transition-colors"
          :class="
            isDirty(it)
              ? 'border-primary/50 bg-primary/10'
              : 'border-base-600/40 bg-base-700/25 hover:border-base-600 hover:bg-base-700/40'
          "
          :title="tipOf(it)"
        >
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5">
              <span
                class="truncate text-[12px] font-medium"
                :class="isDirty(it) ? 'text-primary-light' : 'text-content'"
              >{{ it.label }}</span>
              <Info v-if="it.note" class="h-3 w-3 shrink-0 text-warn/70" />
            </div>
            <div class="mt-0.5 flex items-center gap-1 text-[10px]">
              <span class="truncate font-num text-content-faint">{{ it.key }}</span>
              <span class="shrink-0 text-content-faint/60">·</span>
              <span class="shrink-0 text-content-muted">当前 {{ fmtValue(it) }}</span>
            </div>
          </div>

          <div class="flex shrink-0 items-center gap-1.5 pt-0.5">
            <!-- 开关 -->
            <button
              v-if="it.kind === 'bool'"
              type="button"
              class="relative h-4 w-8 shrink-0 rounded-full transition-colors"
              :class="draft[it.key] ? 'bg-primary' : 'bg-base-600'"
              :title="draft[it.key] ? '点击关闭' : '点击开启'"
              @click="draft[it.key] = !draft[it.key]"
            >
              <span
                class="absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all"
                :class="draft[it.key] ? 'left-[18px]' : 'left-0.5'"
              ></span>
            </button>

            <!-- 数字 -->
            <input
              v-else-if="it.kind === 'number'"
              v-model.number="draft[it.key]"
              type="number"
              step="any"
              class="h-7 w-[84px] shrink-0 rounded border bg-base-800/60 px-1.5 text-right font-num text-[12px] text-content outline-none transition-colors focus:border-primary"
              :class="isDirty(it) ? 'border-primary/60' : 'border-base-600'"
            />

            <!-- 文本 -->
            <input
              v-else-if="it.kind === 'text'"
              v-model="draft[it.key]"
              type="text"
              class="h-7 w-[130px] shrink-0 rounded border bg-base-800/60 px-1.5 text-right font-num text-[12px] text-content outline-none transition-colors focus:border-primary"
              :class="isDirty(it) ? 'border-primary/60' : 'border-base-600'"
            />

            <!-- 只读（路径/列表） -->
            <span
              v-else
              class="shrink-0 cursor-help rounded-full border border-base-600 px-1.5 py-0.5 text-[10px] leading-none text-content-faint"
            >
              需重启
            </span>

            <span
              class="shrink-0 whitespace-nowrap rounded-full border px-1.5 py-0.5 text-[10px] leading-none"
              :class="sourceMeta[it.source]?.cls ?? 'border-base-600 text-content-faint'"
            >
              {{ sourceMeta[it.source]?.text ?? it.source }}
            </span>

            <button
              v-if="isDirty(it)"
              class="shrink-0 rounded p-0.5 text-content-faint transition-colors hover:text-primary"
              title="撤销这一行的改动（不写库）"
              @click="revertRow(it)"
            >
              <RotateCcw class="h-3 w-3" />
            </button>
          </div>
        </div>
      </div>

      <p v-if="!currentItems.length" class="py-6 text-center text-[12px] text-content-faint">
        没有匹配的参数
      </p>
    </div>

    <!-- 底部悬浮保存条：只在有未保存改动时出现。原先只有顶栏一个「保存」按钮，
         开关/数字改完往下翻几屏就看不见它了，容易误以为「没有保存按钮」 -->
    <Teleport to="body">
      <div
        v-if="dirtyKeys.length"
        class="pointer-events-none fixed inset-x-0 bottom-5 z-40 flex justify-center px-4"
      >
        <div
          class="pointer-events-auto flex flex-wrap items-center gap-3 rounded-full border border-primary/40 bg-base-800/95 px-4 py-2 shadow-glow backdrop-blur"
        >
          <span class="text-[12px] text-content">
            有 <span class="font-num font-semibold text-primary-light">{{ dirtyKeys.length }}</span> 项未保存
            <span class="text-content-faint">（保存后立即落库并对下一 tick 生效）</span>
          </span>
          <button
            type="button"
            class="rounded-lg bg-primary px-3 py-1 text-[12px] font-medium text-white transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
            :disabled="saving"
            @click="save"
          >
            {{ saving ? '保存中...' : '保存并生效' }}
          </button>
          <button
            type="button"
            class="rounded-lg border border-base-600 px-3 py-1 text-[12px] text-content-muted transition-colors hover:text-content disabled:opacity-50"
            :disabled="saving"
            @click="discardAll"
          >
            放弃
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>
