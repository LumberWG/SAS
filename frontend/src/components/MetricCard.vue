<script setup lang="ts">
/**
 * 指标卡：标题 + 大数值 + 副文本，支持涨跌配色与点击交互。
 */
defineProps<{
  title: string
  value: string | number
  sub?: string
  /** 数值倾向: up(红) / down(绿) / neutral */
  tone?: 'up' | 'down' | 'neutral'
  clickable?: boolean
}>()

const emit = defineEmits<{ click: [] }>()
</script>

<template>
  <div
    class="glass-card p-4"
    :class="clickable ? 'glass-card-interactive cursor-pointer' : ''"
    @click="clickable && emit('click')"
  >
    <div class="mb-1.5 flex items-center justify-between">
      <span class="text-[13.5px] text-content-muted">{{ title }}</span>
      <slot name="icon" />
    </div>
    <div
      class="font-num text-[26px] font-semibold leading-tight tracking-tight"
      :class="{
        'text-up': tone === 'up',
        'text-down': tone === 'down',
        'text-content': tone !== 'up' && tone !== 'down',
      }"
    >
      {{ value }}
    </div>
    <div v-if="sub" class="mt-1 text-[12.5px] text-content-faint">{{ sub }}</div>
    <slot />
  </div>
</template>
