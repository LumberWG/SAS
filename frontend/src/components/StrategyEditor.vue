<script setup lang="ts">
/**
 * 策略编辑器：按参数 schema 渲染表单，支持改参数另存为自定义策略。
 * 内置策略编辑后必须另存为新策略（不覆盖内置）。
 */
import { computed, reactive, ref, watch } from 'vue'
import { Save, Settings2, X } from 'lucide-vue-next'

import {
  createCustomStrategy,
  deleteCustomStrategy,
  type StrategyInfo,
} from '../api'

const props = defineProps<{
  strategy: StrategyInfo
}>()

const emit = defineEmits<{
  close: []
  saved: [name: string]
  deleted: [name: string]
}>()

// 表单值（用默认值初始化，编辑时实时同步）
const form = reactive<Record<string, number | boolean | string>>({})
const customName = ref('')
const saving = ref(false)
const errorMsg = ref('')

function initForm() {
  for (const [key, spec] of Object.entries(props.strategy.parameters)) {
    form[key] = spec.default
  }
  // 默认另存名：内置策略加后缀，自定义策略保持原名
  customName.value = props.strategy.custom
    ? props.strategy.name
    : `${props.strategy.name}·改`
}
watch(() => props.strategy, initForm, { immediate: true })

const hasParams = computed(
  () => Object.keys(props.strategy.parameters).length > 0
)

async function save() {
  saving.value = true
  errorMsg.value = ''
  try {
    await createCustomStrategy({
      name: customName.value.trim(),
      base_strategy: props.strategy.name,
      params: form,
      description: `${props.strategy.description}（自定义参数版）`,
    })
    emit('saved', customName.value.trim())
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response
      ?.data?.detail
    errorMsg.value = detail ?? '保存失败'
  } finally {
    saving.value = false
  }
}

async function remove() {
  if (!props.strategy.custom) return
  try {
    await deleteCustomStrategy(props.strategy.name)
    emit('deleted', props.strategy.name)
  } catch (e: unknown) {
    const detail = (e as { response?: { data?: { detail?: string } } })?.response
      ?.data?.detail
    errorMsg.value = detail ?? '删除失败'
  }
}

function resetDefaults() {
  initForm()
}
</script>

<template>
  <div
    class="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
    @click.self="emit('close')"
  >
    <div class="glass-card w-full max-w-lg animate-fade-in overflow-hidden !bg-base-800">
      <!-- 标题栏 -->
      <div class="flex items-center justify-between border-b border-base-600/60 px-5 py-4">
        <div class="flex items-center gap-2.5">
          <Settings2 class="h-4 w-4 text-primary-light" />
          <div>
            <h3 class="text-subheading">{{ strategy.name }} · 参数编辑</h3>
            <p class="mt-0.5 text-[11px] text-content-faint">
              {{ strategy.category }} · 最少 {{ strategy.min_bars }} 根K线
              <span
                v-if="strategy.custom"
                class="ml-1 rounded bg-accent/20 px-1.5 text-accent"
                >自定义</span
              >
            </p>
          </div>
        </div>
        <button
          class="rounded-lg p-1.5 text-content-faint transition-colors hover:bg-base-700 hover:text-content"
          @click="emit('close')"
        >
          <X class="h-4 w-4" />
        </button>
      </div>

      <!-- 策略说明 -->
      <div
        class="border-b border-base-600/60 bg-base-700/30 px-5 py-3 text-[12px] leading-relaxed text-content-muted"
      >
        {{ strategy.description }}
      </div>

      <!-- 参数表单 -->
      <div class="max-h-[50vh] space-y-4 overflow-y-auto px-5 py-4">
        <p v-if="!hasParams" class="py-6 text-center text-[13px] text-content-faint">
          该策略无可调参数
        </p>

        <div v-for="(spec, key) in strategy.parameters" :key="key" class="space-y-1.5">
          <!-- 布尔：开关 -->
          <div v-if="spec.type === 'bool'" class="flex items-center justify-between">
            <span class="text-[13px] text-content">{{ spec.label }}</span>
            <button
              class="relative h-5 w-9 rounded-full transition-colors"
              :class="form[key] ? 'bg-primary' : 'bg-base-600'"
              @click="form[key] = !form[key]"
            >
              <span
                class="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all"
                :class="form[key] ? 'left-4.5' : 'left-0.5'"
              />
            </button>
          </div>

          <!-- 下拉：枚举 -->
          <div
            v-else-if="spec.type === 'select'"
            class="flex items-center justify-between gap-3"
          >
            <span class="shrink-0 text-[13px] text-content">{{ spec.label }}</span>
            <select
              :value="form[key]"
              class="rounded-lg border border-base-600 bg-base-700 px-3 py-1.5 text-[13px] text-content outline-none focus:border-primary"
              @change="form[key] = ($event.target as HTMLSelectElement).value"
            >
              <option v-for="opt in spec.options ?? []" :key="opt[0]" :value="opt[0]">
                {{ opt[1] }}
              </option>
            </select>
          </div>

          <!-- 数值：输入框 + 滑块 -->
          <div v-else>
            <div class="flex items-center justify-between">
              <span class="text-[13px] text-content">{{ spec.label }}</span>
              <div class="flex items-center gap-1.5">
                <input
                  type="number"
                  :min="spec.min ?? undefined"
                  :max="spec.max ?? undefined"
                  :step="spec.step ?? undefined"
                  :value="form[key]"
                  class="w-20 rounded-lg border border-base-600 bg-base-700 px-2 py-1 text-right font-num text-[13px] text-content outline-none focus:border-primary"
                  @input="form[key] = Number(($event.target as HTMLInputElement).value)"
                />
                <span v-if="spec.unit" class="w-6 text-[11px] text-content-faint">{{
                  spec.unit
                }}</span>
              </div>
            </div>
            <input
              v-if="
                spec.min !== null &&
                spec.min !== undefined &&
                spec.max !== null &&
                spec.max !== undefined
              "
              type="range"
              :min="spec.min"
              :max="spec.max"
              :step="spec.step ?? 1"
              :value="form[key]"
              class="w-full accent-primary"
              @input="form[key] = Number(($event.target as HTMLInputElement).value)"
            />
          </div>
        </div>
      </div>

      <!-- 底部操作 -->
      <div class="border-t border-base-600/60 px-5 py-4">
        <p v-if="errorMsg" class="mb-2 text-[12px] text-up">{{ errorMsg }}</p>
        <div class="flex items-center gap-2">
          <input
            v-model="customName"
            type="text"
            maxlength="30"
            placeholder="策略名称"
            class="min-w-0 flex-1 rounded-lg border border-base-600 bg-base-700 px-3 py-2 text-[13px] text-content outline-none transition-colors placeholder:text-content-faint focus:border-primary"
          />
          <button
            class="btn btn-primary px-4 py-2 text-[13px]"
            :disabled="saving || !customName.trim() || !hasParams"
            @click="save"
          >
            <Save class="h-3.5 w-3.5" />
            {{ saving ? '保存中...' : strategy.custom ? '更新' : '另存为' }}
          </button>
          <button
            v-if="strategy.custom"
            class="rounded-lg border border-down/40 px-3 py-2 text-[13px] text-down transition-colors hover:bg-down/10"
            @click="remove"
          >
            删除
          </button>
          <button
            class="rounded-lg border border-base-600 px-3 py-2 text-[13px] text-content-muted transition-colors hover:border-primary/50 hover:text-primary-light"
            @click="resetDefaults"
          >
            重置
          </button>
        </div>
        <p class="mt-2 text-[11px] text-content-faint">
          另存为将创建新策略（可重命名），不会修改内置策略；保存后即可在扫描与回测中选择
        </p>
      </div>
    </div>
  </div>
</template>
