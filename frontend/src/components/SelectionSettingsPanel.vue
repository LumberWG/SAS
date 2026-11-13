<script setup lang="ts">
/**
 * 选股策略设置面板：把原本散在「系统设置」里的选股相关参数收拢到「选股策略」(全市场选股) 页。
 *
 * 这里渲染的参数与 `Settings.vue` 同源（后端 `param_specs.selection_settings_overview`），
 * 复用同一套控件：板块多选芯片 / 开关 / 数字框 / 文本框，支持 dirty 高亮、tooltip 完整说明、
 * 条件可见（depends_on）、保存后回读确认。写回走通用的 `updateSettings`（`POST /api/settings`），
 * 落 `app_state` 覆盖层 + 就地改单例，下一 tick / 下一轮扫描即生效。
 */
import { computed, onMounted, ref } from 'vue'
import { Info, RotateCcw, Save, X } from 'lucide-vue-next'

import {
  getSelectionSettings,
  updateSettings,
  type ParamItem,
  type SettingsOverview,
} from '../api'

const emit = defineEmits<{ close: [] }>()

const data = ref<SettingsOverview | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const toast = ref('')
const verify = ref<{ ok: boolean; keys: number; mismatches: string[] } | null>(null)
const flashKeys = ref<string[]>([])
let flashTimer: number | undefined

const draft = ref<Record<string, string | number | boolean>>({})
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
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    hydrate(await getSelectionSettings())
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)

function fmtValue(it: ParamItem): string {
  const v = baseline.value[it.key]
  if (it.kind === 'bool') return v ? '开' : '关'
  if (v === '' || v === null || v === undefined) return '（空）'
  if (Array.isArray(v)) return v.join(', ')
  return String(v)
}

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
  for (const g of data.value?.groups ?? []) for (const it of g.items) if (isDirty(it)) keys.push(it.key)
  return keys
})

const itemByKey = computed(() => {
  const map: Record<string, ParamItem> = {}
  for (const g of data.value?.groups ?? []) for (const it of g.items) map[it.key] = it
  return map
})

function parentSatisfied(parentKey: string): boolean {
  const parent = itemByKey.value[parentKey]
  if (!parent) return true
  const value = draft.value[parentKey]
  if (parent.kind === 'bool') return Boolean(value)
  return String(value ?? '').trim() !== ''
}

function isHidden(it: ParamItem): boolean {
  let cursor: ParamItem | undefined = it
  const seen = new Set<string>()
  while (cursor?.depends_on) {
    if (seen.has(cursor.key)) break
    seen.add(cursor.key)
    if (!parentSatisfied(cursor.depends_on)) return true
    cursor = itemByKey.value[cursor.depends_on]
  }
  return false
}

const visibleGroups = computed(() =>
  (data.value?.groups ?? [])
    .map((g) => ({
      name: g.name,
      desc: g.desc ?? '',
      subgroups: (g.subgroups?.length ? g.subgroups : [{ name: '全部参数', desc: '', items: g.items }])
        .map((sg) => ({
          name: sg.name,
          desc: sg.desc ?? '',
          items: sg.items.filter((it) => !isHidden(it)),
        }))
        .filter((sg) => sg.items.length),
    }))
    .filter((g) => g.subgroups.length),
)

function tipOf(it: ParamItem): string {
  const lines = [it.label]
  if (it.desc) lines.push(it.desc)
  lines.push(it.key, `当前值：${fmtValue(it)}`)
  if (isDirty(it)) {
    const v = draft.value[it.key]
    lines.push(`待保存：${typeof v === 'boolean' ? (v ? '开' : '关') : String(v ?? '')}`)
  }
  if (it.note) lines.push(`补充：${it.note}`)
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
  verify.value = null
  try {
    await updateSettings(changes)
    // 回读确认：覆盖层真的生效了（来源 app_state）
    const fresh = await getSelectionSettings()
    hydrate(fresh)
    const items = new Map<string, ParamItem>()
    for (const g of fresh.groups) for (const it of g.items) items.set(it.key, it)
    const mismatches: string[] = []
    for (const [key, want] of Object.entries(changes)) {
      const it = items.get(key)
      if (!it) {
        mismatches.push(`${key}：回读结果里不存在`)
        continue
      }
      if (it.secret) continue
      const got = it.value
      const same =
        String(got) === String(want) ||
        (Number.isFinite(Number(got)) && Number(got) === Number(want))
      if (!same) {
        mismatches.push(`${key}：期望 ${want}，实际 ${got}（来源 ${it.source}）`)
      } else if (it.source !== 'app_state') {
        mismatches.push(`${key}：值一致，但来源是 ${it.source}，覆盖层未生效`)
      }
    }
    verify.value = { ok: mismatches.length === 0, keys: Object.keys(changes).length, mismatches }
    toast.value = mismatches.length
      ? `已提交 ${Object.keys(changes).length} 项，但有 ${mismatches.length} 项回读未确认`
      : `已回读确认 ${Object.keys(changes).length} 项，来源 app_state，立即生效`
    window.setTimeout(() => (toast.value = ''), 4000)
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : '保存失败'
  } finally {
    saving.value = false
  }
}

function revertRow(it: ParamItem) {
  draft.value[it.key] = baseline.value[it.key]
}

function msSelected(it: ParamItem, opt: string): boolean {
  const cur = String(draft.value[it.key] ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  return cur.includes(opt)
}

function toggleBoard(it: ParamItem, opt: string) {
  const order = (it.options ?? []) as string[]
  const cur = String(draft.value[it.key] ?? '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  const idx = cur.indexOf(opt)
  if (idx >= 0) cur.splice(idx, 1)
  else cur.push(opt)
  cur.sort((a, b) => order.indexOf(a) - order.indexOf(b))
  draft.value[it.key] = cur.join(',')
}

function discardAll() {
  draft.value = { ...baseline.value }
}

function close() {
  if (flashTimer) window.clearTimeout(flashTimer)
  emit('close')
}
</script>

<template>
  <Teleport to="body">
    <div
      class="fixed inset-0 z-[110] flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      @click.self="close"
    >
      <div class="glass-card flex max-h-[88vh] w-full max-w-3xl animate-fade-in flex-col overflow-hidden !bg-base-800">
        <!-- 标题栏 -->
        <div class="flex items-center justify-between border-b border-base-600/60 px-4 py-3 sm:px-5">
          <div class="flex items-center gap-2.5">
            <span class="h-1.5 w-1.5 rounded-full bg-primary"></span>
            <h3 class="text-subheading">选股策略设置</h3>
            <span class="rounded border border-base-600/60 bg-base-700/50 px-1.5 py-px text-[11.5px] text-content-faint">
              保存后即时生效
            </span>
          </div>
          <button
            class="rounded-lg p-1.5 text-content-faint transition-colors hover:bg-base-700 hover:text-content"
            @click="close"
          >
            <X class="h-4 w-4" />
          </button>
        </div>

        <!-- 验证 / 提示 / 错误 -->
        <div v-if="verify" class="mx-4 mt-3 rounded border px-2 py-1.5 text-[12.5px] leading-relaxed"
          :class="verify.ok ? 'border-up/30 bg-up/10 text-up' : 'border-warn/40 bg-warn/10 text-warn'">
          <div class="font-medium">
            {{ verify.ok ? `回读确认通过：${verify.keys} 项的值与来源（app_state）均已核实` : `回读未确认：${verify.mismatches.length} 项` }}
          </div>
          <ul v-if="verify.mismatches.length" class="mt-1 list-disc pl-5">
            <li v-for="m in verify.mismatches" :key="m" class="font-num">{{ m }}</li>
          </ul>
        </div>
        <div v-if="toast" class="mx-4 mt-3 rounded border border-up/30 bg-up/10 px-2 py-1 text-[12.5px] text-up">
          {{ toast }}
        </div>
        <div v-if="error" class="mx-4 mt-3 rounded border border-down/30 bg-down/10 px-2 py-1 text-[12.5px] text-down">
          {{ error }}
        </div>

        <!-- 内容：分组 → 小节 → 参数行 -->
        <div class="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3 sm:px-5">
          <div v-if="loading" class="py-10 text-center text-[13.5px] text-content-faint">加载中…</div>

          <section v-for="g in visibleGroups" :key="g.name" class="rounded-xl border border-base-600/60 bg-base-800/70">
            <header class="flex flex-wrap items-center gap-x-2 border-b border-base-600/50 px-3 py-2.5">
              <h4 class="text-[14.5px] font-semibold text-content">{{ g.name }}</h4>
              <span class="min-w-0 flex-1 truncate text-[12.5px] text-content-faint">{{ g.desc }}</span>
            </header>

            <div v-for="sg in g.subgroups" :key="`${g.name}/${sg.name}`" class="mx-2 my-2 overflow-hidden rounded-lg border border-base-600/40 bg-base-800/40">
              <div class="flex flex-wrap items-center gap-x-2 border-b border-base-600/30 bg-base-700/20 px-2.5 py-1.5">
                <span class="h-3 w-0.5 rounded-full bg-primary/70"></span>
                <span class="text-[13px] font-medium text-content">{{ sg.name }}</span>
                <span class="truncate text-[11.5px] text-content-faint">{{ sg.desc }}</span>
              </div>

              <div class="grid grid-cols-[repeat(auto-fill,minmax(min(296px,100%),1fr))] gap-x-2 p-1.5">
                <div
                  v-for="it in sg.items"
                  :key="it.key"
                  class="flex min-w-0 items-center gap-1.5 rounded-md px-1.5 transition-colors"
                  :class="[
                    it.kind === 'multiselect' ? 'h-auto py-1' : 'h-9',
                    isDirty(it) ? 'bg-primary/[0.10] ring-1 ring-inset ring-primary/25' : 'hover:bg-base-700/50',
                  ]"
                  :title="tipOf(it)"
                >
                  <span
                    class="h-4 w-0.5 shrink-0 rounded-full transition-colors"
                    :class="isDirty(it) ? 'bg-primary' : 'bg-transparent'"
                  ></span>
                  <span
                    class="w-[100px] shrink-0 truncate text-right text-[13.5px] leading-tight"
                    :class="isDirty(it) ? 'font-medium text-primary-light' : 'text-content-muted'"
                  >{{ it.label }}</span>

                  <span
                    class="flex shrink-0 items-center gap-1.5"
                    :class="it.kind === 'multiselect' ? 'w-auto flex-wrap' : 'w-[112px]'"
                  >
                    <template v-if="it.kind === 'bool'">
                      <button
                        type="button"
                        class="relative h-4 w-8 shrink-0 rounded-full transition-colors"
                        :class="draft[it.key] ? 'bg-primary' : 'bg-base-600'"
                        :title="draft[it.key] ? '点击关闭' : '点击开启'"
                        @click="draft[it.key] = !draft[it.key]"
                      >
                        <span class="absolute top-0.5 h-3 w-3 rounded-full bg-white transition-all"
                          :class="draft[it.key] ? 'left-[18px]' : 'left-0.5'"></span>
                      </button>
                      <span class="text-[11.5px] text-content-faint">{{ draft[it.key] ? '开' : '关' }}</span>
                    </template>

                    <input
                      v-else-if="it.kind === 'number'"
                      v-model.number="draft[it.key]"
                      type="number"
                      step="any"
                      class="field field-num h-7 w-full px-1.5 text-[13.5px]"
                      :class="isDirty(it) ? 'border-primary/60' : 'border-base-600'"
                    />

                    <input
                      v-else-if="it.kind === 'text'"
                      v-model="draft[it.key]"
                      type="text"
                      class="field field-num h-7 w-full px-1.5 text-[13.5px]"
                      :class="isDirty(it) ? 'border-primary/60' : 'border-base-600'"
                    />

                    <template v-else-if="it.kind === 'multiselect'">
                      <span class="flex flex-wrap items-center gap-1">
                        <button
                          v-for="opt in it.options"
                          :key="opt"
                          type="button"
                          class="rounded border px-1.5 py-px text-[12px] leading-4 transition-colors"
                          :class="msSelected(it, opt)
                            ? 'border-primary/60 bg-primary/15 text-primary-light'
                            : 'border-base-600 bg-base-700/40 text-content-faint hover:text-content'"
                          :title="msSelected(it, opt) ? '点击取消勾选' : '点击勾选'"
                          @click="toggleBoard(it, opt)"
                        >{{ opt }}</button>
                      </span>
                    </template>

                    <span v-else class="text-[11.5px] text-content-faint">需重启</span>
                  </span>

                  <span class="ml-1 flex shrink-0 items-center gap-1">
                    <span
                      class="rounded-full border px-1 py-px text-[11px] leading-none"
                      :class="sourceMeta[it.source]?.cls ?? 'text-content-faint'"
                    >{{ sourceMeta[it.source]?.text ?? it.source }}</span>
                    <button
                      v-if="isDirty(it)"
                      class="rounded p-0.5 text-content-faint transition-colors hover:text-primary"
                      title="撤销这一行的改动（不写库）"
                      @click="revertRow(it)"
                    >
                      <RotateCcw class="h-3 w-3" />
                    </button>
                  </span>
                </div>
              </div>
            </div>
          </section>

          <p v-if="!loading && !visibleGroups.length" class="py-10 text-center text-[13.5px] text-content-muted">
            没有可显示的选股参数
          </p>
        </div>

        <!-- 底部操作 -->
        <div class="flex items-center justify-between gap-3 border-t border-base-600/60 px-4 py-3 sm:px-5">
          <span class="flex items-center gap-1 text-[11.5px] text-content-faint">
            <Info class="h-3 w-3" />悬停任一行可看完整 key、当前值与说明
          </span>
          <div class="flex items-center gap-2">
            <button
              class="btn btn-outline"
              :disabled="saving || !dirtyKeys.length"
              @click="discardAll"
            >
              放弃
            </button>
            <button
              class="btn btn-primary"
              :disabled="!dirtyKeys.length || saving"
              :title="dirtyKeys.length ? '写入 app_state 覆盖层并立即生效（重启保留）' : '当前没有改动'"
              @click="save"
            >
              <Save class="h-3.5 w-3.5" />
              保存{{ dirtyKeys.length ? ` (${dirtyKeys.length})` : '' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>
