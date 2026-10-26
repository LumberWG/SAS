<script setup lang="ts">
/**
 * 通用确认对话框：替代浏览器原生 confirm，样式与站点一致。
 * 用法：<ConfirmDialog :open="..." title="..." message="..." @confirm="..." @cancel="..." />
 */
import { onUnmounted, watch } from 'vue'
import { AlertTriangle } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    message: string
    /** 次要说明（灰色小字） */
    hint?: string
    confirmText?: string
    cancelText?: string
    /** 确认按钮色调：primary 普通 / up 买入类 / down 危险类 */
    tone?: 'primary' | 'up' | 'down'
    /** 处理中：禁用按钮并显示「处理中...」 */
    loading?: boolean
  }>(),
  {
    hint: '',
    confirmText: '确定',
    cancelText: '取消',
    tone: 'primary',
    loading: false,
  }
)

const emit = defineEmits<{ confirm: []; cancel: [] }>()

const toneClass = {
  primary: 'bg-primary hover:brightness-110',
  up: 'bg-up hover:brightness-110',
  down: 'bg-down hover:brightness-110',
} as const

function onKeydown(e: KeyboardEvent) {
  if (!props.open) return
  if (e.key === 'Escape') emit('cancel')
  if (e.key === 'Enter') emit('confirm')
}

watch(
  () => props.open,
  (open) => {
    if (open) window.addEventListener('keydown', onKeydown)
    else window.removeEventListener('keydown', onKeydown)
  },
  { immediate: true }
)

onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
      @click.self="emit('cancel')"
    >
      <div class="glass-card w-full max-w-sm animate-fade-in overflow-hidden !bg-base-800">
        <div class="flex items-center gap-2.5 border-b border-base-600/60 px-5 py-4">
          <AlertTriangle
            class="h-4 w-4"
            :class="tone === 'down' ? 'text-down' : tone === 'up' ? 'text-up' : 'text-primary-light'"
          />
          <h3 class="text-subheading">{{ title }}</h3>
        </div>

        <div class="px-5 py-4">
          <p class="text-[13px] leading-relaxed text-content-muted">{{ message }}</p>
          <p v-if="hint" class="mt-2 text-[11px] leading-relaxed text-content-faint">{{ hint }}</p>
        </div>

        <div class="flex items-center justify-end gap-2 border-t border-base-600/60 px-5 py-3">
          <button
            class="rounded-lg border border-base-600 px-3 py-1.5 text-[12px] text-content-muted transition-colors hover:text-content disabled:opacity-50"
            :disabled="loading"
            @click="emit('cancel')"
          >
            {{ cancelText }}
          </button>
          <button
            class="rounded-lg px-3.5 py-1.5 text-[12px] font-medium text-white transition-all disabled:cursor-not-allowed disabled:opacity-50"
            :class="toneClass[tone]"
            :disabled="loading"
            @click="emit('confirm')"
          >
            {{ loading ? '处理中...' : confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
