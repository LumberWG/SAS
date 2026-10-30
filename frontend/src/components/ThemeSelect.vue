<script setup lang="ts">
/**
 * 主题选择器：下拉控件 + 配色色块。
 *
 * 为什么不用原生 `<select>`：原生 option 里塞不进色块，只能显示文字，四套主题
 * 在列表里长得一模一样。这里做成「按钮 + 悬浮菜单」，每个选项左侧仍是那四格
 * 预览色（页面底 / 卡片 / 主色 / 文字），收起时按钮上也保留当前主题的色块。
 *
 * 菜单用 Teleport 挂到 body：设置页顶栏是 `overflow-hidden` 的卡片，直接定位会被裁掉。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, ChevronDown, Palette } from 'lucide-vue-next'

import { THEMES, useThemeStore, type ThemeKey } from '../stores/theme'

const emit = defineEmits<{ change: [name: string] }>()

const theme = useThemeStore()
const open = ref(false)
const trigger = ref<HTMLElement | null>(null)
const pos = ref({ left: 0, top: 0 })

const current = computed(() => theme.meta)

function place() {
  const el = trigger.value
  if (!el) return
  const rect = el.getBoundingClientRect()
  // 菜单右对齐按钮，并保证不越过视口右边
  pos.value = { left: Math.max(8, rect.right - 208), top: rect.bottom + 6 }
}

function toggle() {
  open.value = !open.value
  if (open.value) place()
}

function pick(key: ThemeKey) {
  theme.set(key)
  open.value = false
  emit('change', theme.meta.name)
}

function onDocClick(event: MouseEvent) {
  if (!open.value) return
  const target = event.target as HTMLElement
  if (trigger.value?.contains(target) || target.closest('[data-theme-menu]')) return
  open.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', place)
  window.addEventListener('scroll', place, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', place)
  window.removeEventListener('scroll', place, true)
})
</script>

<template>
  <div
    class="flex items-center gap-1.5 rounded-lg border border-primary/25 bg-primary/[0.06] px-2 py-1"
    title="只影响本机浏览器显示，不写入后端配置"
  >
    <Palette class="h-3.5 w-3.5 shrink-0 text-primary-light" />
    <span class="text-[11.5px] text-content-muted">主题</span>

    <button
      ref="trigger"
      type="button"
      class="flex items-center gap-1.5 rounded border border-base-600 bg-base-800 px-1.5 py-0.5 text-[12.5px] text-content transition-colors hover:border-primary/50"
      :aria-expanded="open"
      aria-haspopup="listbox"
      aria-label="界面主题"
      @click.stop="toggle"
    >
      <span class="flex gap-px" aria-hidden="true">
        <span
          v-for="(color, i) in current.swatch"
          :key="i"
          class="h-3 w-2 rounded-[1px] border border-black/10"
          :style="{ backgroundColor: color }"
        ></span>
      </span>
      {{ current.name }}
      <ChevronDown class="h-3 w-3 transition-transform" :class="open ? 'rotate-180' : ''" />
    </button>
  </div>

  <Teleport to="body">
    <div
      v-if="open"
      data-theme-menu
      role="listbox"
      class="fixed z-[120] w-52 overflow-hidden rounded-lg border border-base-600 bg-base-800 p-1 shadow-pop"
      :style="{ left: `${pos.left}px`, top: `${pos.top}px` }"
    >
      <button
        v-for="item in THEMES"
        :key="item.key"
        type="button"
        role="option"
        :aria-selected="theme.current === item.key"
        class="flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-base-700/70"
        @click="pick(item.key)"
      >
        <span class="flex shrink-0 gap-px" aria-hidden="true">
          <span
            v-for="(color, i) in item.swatch"
            :key="i"
            class="h-4 w-2.5 rounded-[1px] border border-black/10"
            :style="{ backgroundColor: color }"
          ></span>
        </span>
        <span
          class="flex-1 text-[12.5px]"
          :class="theme.current === item.key ? 'font-medium text-primary-light' : 'text-content'"
        >{{ item.name }}</span>
        <span class="shrink-0 text-[10.5px] text-content-faint">{{ item.dark ? '深色' : '浅色' }}</span>
        <Check v-if="theme.current === item.key" class="h-3.5 w-3.5 shrink-0 text-primary" />
      </button>
      <div class="mt-1 border-t border-base-600/50 px-2 pt-1 text-[10.5px] text-content-faint">
        仅本机浏览器，换设备回到默认
      </div>
    </div>
  </Teleport>
</template>
