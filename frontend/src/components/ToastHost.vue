<script setup lang="ts">
/**
 * 全局 Toast 容器：渲染 notify store 中的提示。
 * 放在 App.vue 根级，任意页面触发的提示都会在此展示。
 *
 * 深色底 + 左侧语义色描边（不再用浅色 alert 配色，否则深色界面里蹦出白底块很刺眼）。
 */
import { useNotifyStore } from '../stores/notify'
import { AlertTriangle, CheckCircle2, Info, XCircle } from 'lucide-vue-next'

const notify = useNotifyStore()

const icons = {
  warn: AlertTriangle,
  error: XCircle,
  info: Info,
  success: CheckCircle2,
}

const toneBorder: Record<string, string> = {
  warn: 'border-warn/45',
  error: 'border-up/45',
  info: 'border-primary/45',
  success: 'border-down/45',
}

const toneText: Record<string, string> = {
  warn: 'text-warn',
  error: 'text-up',
  info: 'text-primary-light',
  success: 'text-down',
}
</script>

<template>
  <div class="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 flex-col gap-2">
    <transition-group name="toast">
      <div
        v-for="t in notify.toasts"
        :key="t.id"
        class="pointer-events-auto flex items-start gap-2 rounded-xl border bg-base-800/95 px-3 py-2 text-[12.5px] shadow-pop backdrop-blur"
        :class="toneBorder[t.type]"
      >
        <component :is="icons[t.type]" class="mt-0.5 h-4 w-4 shrink-0" :class="toneText[t.type]" />
        <span class="flex-1 leading-snug text-content">{{ t.message }}</span>
        <button
          class="shrink-0 rounded p-0.5 text-[11px] text-content-faint transition-colors hover:text-content"
          @click="notify.dismiss(t.id)"
        >
          ✕
        </button>
      </div>
    </transition-group>
  </div>
</template>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition: all 0.25s ease;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateX(20px);
}
</style>
