<script setup lang="ts">
/**
 * 全局 Toast 容器：渲染 notify store 中的提示。
 * 放在 App.vue 根级，任意页面触发的提示都会在此展示。
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
</script>

<template>
  <div class="pointer-events-none fixed right-4 top-4 z-[100] flex w-80 flex-col gap-2">
    <transition-group name="toast">
      <div
        v-for="t in notify.toasts"
        :key="t.id"
        class="pointer-events-auto flex items-start gap-2 rounded-lg border px-3 py-2 text-[13px] shadow-lg"
        :class="{
          'border-amber-300 bg-amber-50 text-amber-800': t.type === 'warn',
          'border-red-300 bg-red-50 text-red-700': t.type === 'error',
          'border-sky-300 bg-sky-50 text-sky-700': t.type === 'info',
          'border-emerald-300 bg-emerald-50 text-emerald-700': t.type === 'success',
        }"
      >
        <component :is="icons[t.type]" class="mt-0.5 h-4 w-4 shrink-0" />
        <span class="flex-1 leading-snug">{{ t.message }}</span>
        <button
          class="shrink-0 text-xs opacity-50 transition hover:opacity-100"
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
