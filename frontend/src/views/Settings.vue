<script setup lang="ts">
/**
 * 系统设置：左导航（分组）+ 右侧小节卡片（组内二级分组）。
 *
 * 为什么分两层：参数按前缀天然是「`paper_` 一坨 50 项、其余一两项」，一条平铺的
 * tab 会让一半页面在翻屏、另一半在留白。后端 `param_specs._LAYOUT` 按业务语义切成
 * 8 个分组（每组 7~13 项），组内再切 2~5 个语义小节 —— 这里只负责按层级渲染。
 *
 * 写模型（后端 `app/param_specs.py`）：改动落 `app_state` 覆盖层，同时就地改
 * `Settings` 单例 → 下一个 tick / 下一轮扫描即生效；重启时由启动流程重放，不会丢。
 *
 * 只读项：路径 / 列表类（`kline_dir`、`db_path`、`cors_origins` 等），它们在进程
 * 启动时就被静态挂载、CORS、缓存目录捕获，运行中改只会造成「显示改了实际没变」，
 * 后端压根不返回它们。
 */
import { computed, onMounted, ref } from 'vue'
import { Info, RefreshCw, RotateCcw, Save, Search } from 'lucide-vue-next'

import {
  getSettingsOverview,
  resetSettings,
  updateSettings,
  type ParamGroup,
  type ParamItem,
  type SettingsOverview,
} from '../api'
import { THEMES, useThemeStore, type ThemeKey } from '../stores/theme'

const data = ref<SettingsOverview | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const toast = ref('')
const keyword = ref('')
const activeGroup = ref('')
const onlyChanged = ref(false)

/** 外观主题：本地偏好，存 localStorage（不进 app_state，也不影响后端） */
const theme = useThemeStore()
function pickTheme(key: ThemeKey) {
  theme.set(key)
  toast.value = `已切换为「${theme.meta.name}」主题`
  window.setTimeout(() => (toast.value = ''), 2000)
}

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

/** 兼容旧后端：没有 subgroups 时退化成「全部参数」单个小节 */
function subsOf(g: ParamGroup) {
  return g.subgroups?.length ? g.subgroups : [{ name: '全部参数', desc: '', items: g.items }]
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

const kw = computed(() => keyword.value.trim().toLowerCase())
/** 有筛选条件时跨分组搜索，否则只看当前分组 */
const searching = computed(() => Boolean(kw.value) || onlyChanged.value)

function matches(it: ParamItem): boolean {
  if (onlyChanged.value && it.source !== 'app_state') return false
  const k = kw.value
  if (!k) return true
  return (
    it.key.toLowerCase().includes(k) ||
    it.label.toLowerCase().includes(k) ||
    fmtValue(it).toLowerCase().includes(k) ||
    (it.desc ?? '').toLowerCase().includes(k)
  )
}

/** 左侧导航：每组的总数 / 当前筛选命中数 / 已覆盖数 / 未保存数 */
const navGroups = computed(() =>
  (data.value?.groups ?? []).map((g) => ({
    name: g.name,
    desc: g.desc ?? '',
    total: g.items.length,
    matched: g.items.filter((it) => matches(it)).length,
    covered: g.items.filter((it) => it.source === 'app_state').length,
    dirty: g.items.filter((it) => isDirty(it)).length,
  })),
)

/** 右侧内容：无筛选时只渲染当前分组；搜索/筛选时按分组分段展示全部命中 */
const visibleGroups = computed(() => {
  const all = data.value?.groups ?? []
  const list = searching.value ? all : all.filter((g) => g.name === activeGroup.value)
  return list
    .map((g) => ({
      name: g.name,
      desc: g.desc ?? '',
      total: g.items.length,
      covered: g.items.filter((it) => it.source === 'app_state').length,
      dirty: g.items.filter((it) => isDirty(it)).length,
      subgroups: subsOf(g)
        .map((sg) => ({ name: sg.name, desc: sg.desc ?? '', items: sg.items.filter((it) => matches(it)) }))
        .filter((sg) => sg.items.length),
    }))
    .filter((g) => g.subgroups.length)
})

const visibleCount = computed(() =>
  visibleGroups.value.reduce((n, g) => n + g.subgroups.reduce((m, sg) => m + sg.items.length, 0), 0),
)

const coveredTotal = computed(() => data.value?.runtime_overrides ?? 0)

/**
 * 行悬停提示：完整说明 + 完整 key + 当前值 + 待保存值。
 *
 * 卡片里只留短标签（"固定止损"这种），`config.py` 里那整句注释走 tooltip ——
 * 否则一条 `paper_exclude_large_cap` 的备注就能把卡片撑成三行、网格高低不齐。
 */
function tipOf(it: ParamItem): string {
  const lines = [it.label]
  if (it.desc) lines.push(it.desc)
  lines.push(it.key, `当前值：${fmtValue(it)}`)
  if (isDirty(it)) {
    const v = draft.value[it.key]
    lines.push(`待保存：${typeof v === 'boolean' ? (v ? '开' : '关') : String(v ?? '')}`)
  }
  if (it.note) lines.push(`补充：${it.note}`)
  if (!it.writable) lines.push('路径/列表类参数：仅启动时读取，需改 .env 后重启后端')
  return lines.join('\n')
}

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

/** 整组还原：只还原该组里被 app_state 覆盖过的项（没覆盖过的本来就是默认值） */
function resetGroup(name: string) {
  const g = (data.value?.groups ?? []).find((x) => x.name === name)
  const keys = (g?.items ?? []).filter((it) => it.source === 'app_state').map((it) => it.key)
  if (keys.length) resetKeys(keys)
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
    <!-- 顶部概览：标题 + 生效口径 + 指标 + 操作 -->
    <div class="glass-card relative overflow-hidden px-4 py-3">
      <div
        class="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/70 to-transparent"
      ></div>
      <div class="flex flex-wrap items-start gap-x-4 gap-y-3">
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-2">
            <span class="h-1.5 w-1.5 rounded-full bg-primary"></span>
            <h2 class="text-[15.5px] font-semibold tracking-wide text-content">系统设置</h2>
            <span class="rounded border border-base-600/60 bg-base-700/50 px-1.5 py-px text-[11.5px] text-content-faint">
              保存后即时生效
            </span>
          </div>
          <p class="mt-1 max-w-[780px] text-[12.5px] leading-relaxed text-content-faint">
            {{ data?.hint ?? '加载中…' }}
          </p>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <div class="flex items-center gap-1.5 rounded-lg border border-base-600/60 bg-base-700/30 px-2.5 py-1">
            <span class="font-num text-[16.5px] font-semibold leading-none text-content">{{ data?.total ?? '--' }}</span>
            <span class="text-[11.5px] text-content-faint">可调参数</span>
          </div>
          <div class="flex items-center gap-1.5 rounded-lg border border-primary/40 bg-primary/10 px-2.5 py-1">
            <span class="font-num text-[16.5px] font-semibold leading-none text-primary-light">{{ coveredTotal }}</span>
            <span class="text-[11.5px] text-content-faint">已覆盖</span>
          </div>
          <div
            class="flex items-center gap-1.5 rounded-lg border px-2.5 py-1 transition-colors"
            :class="
              dirtyKeys.length
                ? 'border-warn/50 bg-warn/10'
                : 'border-base-600/60 bg-base-700/30'
            "
          >
            <span
              class="font-num text-[16.5px] font-semibold leading-none"
              :class="dirtyKeys.length ? 'text-warn' : 'text-content'"
            >{{ dirtyKeys.length }}</span>
            <span class="text-[11.5px] text-content-faint">未保存</span>
          </div>

          <div class="mx-0.5 hidden h-6 w-px bg-base-600/60 sm:block"></div>

          <button
            class="btn btn-primary"
            :disabled="!dirtyKeys.length || saving"
            :title="dirtyKeys.length ? '写入 app_state 覆盖层并立即生效（重启保留）' : '当前没有改动'"
            @click="save"
          >
            <Save class="h-3.5 w-3.5" />
            保存{{ dirtyKeys.length ? ` (${dirtyKeys.length})` : '' }}
          </button>
          <button
            class="btn btn-danger"
            :disabled="saving || !coveredTotal"
            title="把所有已覆盖项还原为 .env / 环境变量里的值"
            @click="resetKeys(dirtyKeys.length ? dirtyKeys : [])"
          >
            <RotateCcw class="h-3.5 w-3.5" />还原
          </button>
          <button
            class="btn btn-outline px-2"
            :disabled="loading"
            title="重新拉取当前生效值（丢弃未保存草稿）"
            @click="load"
          >
            <RefreshCw class="h-3.5 w-3.5" :class="loading ? 'animate-spin' : ''" />
          </button>
        </div>
      </div>

      <div v-if="toast" class="mt-2 rounded border border-down/30 bg-down/10 px-2 py-1 text-[12.5px] text-down">
        {{ toast }}
      </div>
      <div v-if="error" class="mt-2 rounded border border-up/30 bg-up/10 px-2 py-1 text-[12.5px] text-up">
        {{ error }}
      </div>
    </div>

    <!-- 外观主题：纯前端偏好（localStorage），换主题即时生效、不动后端配置 -->
    <section class="rounded-xl border border-base-600/70 bg-base-800/70 p-3">
      <header class="mb-2.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        <span class="h-1.5 w-1.5 rounded-full bg-primary"></span>
        <h3 class="panel-title">外观主题</h3>
        <span class="text-[13.5px] text-content-faint">
          只影响当前浏览器显示，不写入后端配置；换电脑会回到默认主题
        </span>
        <span class="flex-1"></span>
        <span class="chip chip-muted">当前：{{ theme.meta.name }}</span>
      </header>
      <div class="grid grid-cols-2 gap-2 md:grid-cols-4">
        <button
          v-for="t in THEMES"
          :key="t.key"
          type="button"
          class="rounded-lg border p-2 text-left transition-colors"
          :class="
            theme.current === t.key
              ? 'border-primary/60 bg-primary/[0.08]'
              : 'border-base-600 hover:border-base-500'
          "
          @click="pickTheme(t.key)"
        >
          <div class="flex gap-1">
            <span
              v-for="(sw, i) in t.swatch"
              :key="i"
              class="h-6 flex-1 rounded border border-black/10"
              :style="{ backgroundColor: sw }"
            ></span>
          </div>
          <div class="mt-1.5 flex items-center gap-1.5">
            <span
              class="text-[14.5px] font-medium"
              :class="theme.current === t.key ? 'text-primary-light' : 'text-content'"
            >{{ t.name }}</span>
            <span v-if="theme.current === t.key" class="chip chip-primary">使用中</span>
          </div>
          <p class="mt-0.5 text-[13px] leading-snug text-content-faint">{{ t.desc }}</p>
        </button>
      </div>
    </section>

    <!-- 左导航 + 右侧小节卡片 -->
    <div class="grid items-start gap-3 lg:grid-cols-[232px_minmax(0,1fr)]">
      <!-- 左栏：搜索 + 分组导航 -->
      <aside class="overflow-hidden rounded-xl border border-base-600/60 bg-base-800/70 backdrop-blur-md lg:sticky lg:top-3">
        <div class="border-b border-base-600/50 p-2">
          <label
            class="flex items-center gap-1.5 rounded-lg border border-base-600 bg-base-700/60 px-2 py-1.5 transition-colors focus-within:border-primary"
          >
            <Search class="h-3.5 w-3.5 shrink-0 text-content-faint" />
            <input
              v-model="keyword"
              placeholder="搜标签 / key / 值 / 说明"
              class="min-w-0 flex-1 bg-transparent text-[13.5px] text-content outline-none placeholder:text-content-faint"
            />
            <button
              v-if="keyword"
              type="button"
              class="shrink-0 text-[14.5px] leading-none text-content-faint transition-colors hover:text-content"
              title="清空搜索"
              @click="keyword = ''"
            >
              ×
            </button>
          </label>
          <button
            class="mt-1.5 flex w-full items-center justify-between rounded-lg border px-2 py-1.5 text-[12.5px] transition-colors"
            :class="
              onlyChanged
                ? 'border-primary/60 bg-primary/15 text-primary-light'
                : 'border-base-600 bg-base-700/60 text-content-muted hover:text-content'
            "
            @click="onlyChanged = !onlyChanged"
          >
            <span>只看已覆盖</span>
            <span class="font-num text-[11.5px]">{{ coveredTotal }}</span>
          </button>
        </div>

        <nav class="max-h-[calc(100vh-260px)] space-y-0.5 overflow-y-auto p-1.5">
          <button
            v-for="g in navGroups"
            :key="g.name"
            type="button"
            class="relative flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors"
            :class="
              !searching && activeGroup === g.name
                ? 'bg-primary/[0.12] text-primary-light'
                : 'text-content-muted hover:bg-base-700/60 hover:text-content'
            "
            @click="activeGroup = g.name; keyword = ''; onlyChanged = false"
          >
            <span
              class="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full transition-colors"
              :class="!searching && activeGroup === g.name ? 'bg-primary' : 'bg-transparent'"
            ></span>
            <span
              class="min-w-0 flex-1 truncate pl-1 text-[13.5px]"
              :class="!searching && activeGroup === g.name ? 'font-medium' : ''"
            >{{ g.name }}</span>
            <span v-if="g.dirty" class="h-1.5 w-1.5 shrink-0 rounded-full bg-warn" title="本组有未保存改动"></span>
            <span
              v-if="g.covered"
              class="shrink-0 rounded bg-primary/20 px-1 font-num text-[11px] leading-4 text-primary-light"
              title="该组已覆盖（app_state）的项数"
            >{{ g.covered }}</span>
            <span
              class="w-6 shrink-0 text-right font-num text-[11.5px]"
              :class="searching && g.matched ? 'text-primary-light' : 'text-content-faint'"
            >{{ searching ? g.matched : g.total }}</span>
          </button>
        </nav>

        <div class="flex items-center justify-between border-t border-base-600/50 px-3 py-1.5 text-[11.5px] text-content-faint">
          <span>{{ navGroups.length }} 个分组</span>
          <span v-if="searching" class="font-num text-primary-light">命中 {{ visibleCount }}</span>
          <span v-else>共 {{ data?.total ?? 0 }} 项</span>
        </div>
      </aside>

      <!-- 右栏 -->
      <div class="min-w-0 space-y-3">
        <div
          v-if="searching"
          class="flex items-center gap-2 rounded-lg border border-primary/25 bg-primary/[0.06] px-3 py-1.5 text-[12.5px] text-content-muted"
        >
          <Search class="h-3 w-3 text-primary-light" />
          在全部 {{ data?.total ?? 0 }} 项中命中
          <span class="font-num font-semibold text-primary-light">{{ visibleCount }}</span> 项
          <span class="text-content-faint">· 点左栏分组可回到分组视图</span>
        </div>

        <section v-for="g in visibleGroups" :key="g.name" class="rounded-xl border border-base-600/60 bg-base-800/70">
          <!-- 分组标题 -->
          <header class="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-base-600/50 px-3 py-2.5">
            <h3 class="text-[14.5px] font-semibold text-content">{{ g.name }}</h3>
            <span class="rounded bg-base-600/40 px-1.5 py-0.5 font-num text-[11.5px] text-content-muted">{{ g.total }}</span>
            <span v-if="g.covered" class="rounded bg-primary/20 px-1.5 py-0.5 text-[11.5px] text-primary-light">
              已覆盖 {{ g.covered }}
            </span>
            <span v-if="g.dirty" class="rounded bg-warn/15 px-1.5 py-0.5 text-[11.5px] text-warn">
              已改未存 {{ g.dirty }}
            </span>
            <span class="min-w-0 flex-1 truncate text-[12.5px] text-content-faint">{{ g.desc }}</span>
            <button
              v-if="g.covered"
              class="btn btn-xs btn-danger shrink-0"
              :disabled="saving"
              title="把本组已覆盖的项还原为 .env 值"
              @click="resetGroup(g.name)"
            >
              <RotateCcw class="h-2.5 w-2.5" />本组还原
            </button>
          </header>

          <!-- 组内小节 -->
          <div
            v-for="sg in g.subgroups"
            :key="`${g.name}/${sg.name}`"
            class="mx-2 my-2 overflow-hidden rounded-lg border border-base-600/40 bg-base-800/40"
          >
            <div class="flex flex-wrap items-center gap-x-2 border-b border-base-600/30 bg-base-700/20 px-2.5 py-1.5">
              <span class="h-3 w-0.5 rounded-full bg-primary/70"></span>
              <span class="text-[13px] font-medium text-content">{{ sg.name }}</span>
              <span class="min-w-0 flex-1 truncate text-[11.5px] text-content-faint">{{ sg.desc }}</span>
              <span class="shrink-0 font-num text-[11.5px] text-content-faint">{{ sg.items.length }}</span>
            </div>

            <!-- 参数行：短名 + 控件一行一个；完整说明 / key / 当前值 / 待保存值都在 tooltip -->
            <div class="grid grid-cols-1 gap-x-4 p-1.5 md:grid-cols-2 2xl:grid-cols-3">
              <div
                v-for="it in sg.items"
                :key="it.key"
                class="flex h-9 items-center gap-1.5 rounded-md px-2 transition-colors"
                :class="
                  isDirty(it)
                    ? 'bg-primary/[0.10] ring-1 ring-inset ring-primary/25'
                    : 'hover:bg-base-700/50'
                "
                :title="tipOf(it)"
              >
                <span
                  class="h-4 w-0.5 shrink-0 rounded-full transition-colors"
                  :class="isDirty(it) ? 'bg-primary' : 'bg-transparent'"
                ></span>
                <span
                  class="min-w-0 flex-1 truncate text-[13.5px]"
                  :class="isDirty(it) ? 'font-medium text-primary-light' : 'text-content-muted'"
                >{{ it.label }}</span>
                <Info v-if="it.note" class="h-3 w-3 shrink-0 text-warn/70" />

                <span
                  class="shrink-0 rounded-full border px-1 py-px text-[11px] leading-none"
                  :class="sourceMeta[it.source]?.cls ?? 'text-content-faint'"
                >{{ sourceMeta[it.source]?.text ?? it.source }}</span>

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
                  class="field field-num h-7 w-[80px] shrink-0 px-1.5 text-[13.5px]"
                  :class="isDirty(it) ? 'border-primary/60' : 'border-base-600'"
                />

                <!-- 文本 -->
                <input
                  v-else-if="it.kind === 'text'"
                  v-model="draft[it.key]"
                  type="text"
                  class="field field-num h-7 w-[120px] shrink-0 px-1.5 text-[13.5px]"
                  :class="isDirty(it) ? 'border-primary/60' : 'border-base-600'"
                />

                <span v-else class="shrink-0 text-[11.5px] text-content-faint">需重启</span>

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
        </section>

        <div
          v-if="!visibleGroups.length"
          class="rounded-xl border border-dashed border-base-600/60 bg-base-800/40 px-4 py-10 text-center"
        >
          <p class="text-[13.5px] text-content-muted">没有匹配的参数</p>
          <p class="mt-1 text-[12.5px] text-content-faint">换个关键词，或点左栏分组回到分组视图</p>
        </div>

        <p class="flex items-center gap-1 px-1 text-[11.5px] text-content-faint">
          <Info class="h-3 w-3" />悬停任一行可看完整 key、当前值与 config.py 原始说明
        </p>
      </div>
    </div>

    <!-- 底部悬浮保存条：只在有未保存改动时出现。原先只有顶栏一个「保存」按钮，
         开关/数字改完往下翻几屏就看不见它了，容易误以为「没有保存按钮」 -->
    <Teleport to="body">
      <div
        v-if="dirtyKeys.length"
        class="pointer-events-none fixed inset-x-0 bottom-5 z-40 flex justify-center px-4"
      >
        <div
          class="pointer-events-auto flex flex-wrap items-center gap-3 rounded-full border border-primary/35 bg-base-800/95 px-4 py-2 shadow-pop backdrop-blur"
        >
          <span class="text-[13.5px] text-content">
            有 <span class="font-num font-semibold text-primary-light">{{ dirtyKeys.length }}</span> 项未保存
            <span class="text-content-faint">（保存后立即落库并对下一 tick 生效）</span>
          </span>
          <button type="button" class="btn btn-primary" :disabled="saving" @click="save">
            {{ saving ? '保存中...' : '保存并生效' }}
          </button>
          <button type="button" class="btn btn-outline" :disabled="saving" @click="discardAll">
            放弃
          </button>
        </div>
      </div>
    </Teleport>
  </div>
</template>
