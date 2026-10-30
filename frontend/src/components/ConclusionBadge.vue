<script setup lang="ts">
/**
 * 结论徽标：买入(红) / 持有(黄) / 卖出(绿)。
 */
import { computed } from 'vue'

const props = defineProps<{
  conclusion: string
  size?: 'sm' | 'lg'
}>()

const cls = computed(() => {
  const base = props.size === 'lg' ? 'px-4 py-1.5 text-[16.5px]' : 'px-2.5 py-0.5 text-[12.5px]'
  switch (props.conclusion) {
    case '买入':
      return `${base} border-up/50 bg-up/15 text-up`
    case '卖出':
      return `${base} border-down/50 bg-down/15 text-down`
    default:
      return `${base} border-warn/50 bg-warn/15 text-warn`
  }
})
</script>

<template>
  <span
    class="inline-flex items-center rounded-full border font-semibold"
    :class="cls"
  >
    {{ conclusion }}
  </span>
</template>
