<script setup lang="ts">
/**
 * 访问令牌输入浮层。
 *
 * 后端 `.env` 配了 `API_TOKEN` 的实例，未带令牌的请求会 401；此时弹这个框，
 * 输入后写进 localStorage 并刷新页面（重新拉一遍所有接口，比逐个重试简单可靠）。
 *
 * 为什么不是登录系统：这是单人自用工具，没有账号体系。一个部署级口令足够挡住
 * 「拿到 cpolar 地址就能改交易参数」这种情况，不值得为此引入用户表与 JWT。
 */
import { ref } from 'vue'
import { KeyRound } from 'lucide-vue-next'

import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const input = ref('')

function submit() {
  const value = input.value.trim()
  if (!value) return
  auth.setToken(value)
  window.location.reload()
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="auth.needed"
      class="fixed inset-0 z-[200] flex items-center justify-center bg-black/70 p-4 backdrop-blur-sm"
    >
      <div class="w-full max-w-sm overflow-hidden rounded-xl border border-base-600 bg-base-800 shadow-pop">
        <div class="flex items-center gap-2 border-b border-base-600/60 px-4 py-3">
          <span class="flex h-7 w-7 items-center justify-center rounded-lg bg-primary/15">
            <KeyRound class="h-4 w-4 text-primary-light" />
          </span>
          <div>
            <h3 class="text-[14px] font-semibold text-content">需要访问令牌</h3>
            <p class="text-[12px] text-content-faint">该实例启用了 API_TOKEN</p>
          </div>
        </div>
        <div class="space-y-2 px-4 py-3">
          <p class="text-[12.5px] leading-relaxed text-content-muted">
            请输入后端 <code class="rounded bg-base-700 px-1 font-num text-[12px]">.env</code> 中
            <code class="rounded bg-base-700 px-1 font-num text-[12px]">API_TOKEN</code> 的值。
            令牌只保存在本机浏览器。
          </p>
          <input
            v-model="input"
            type="password"
            autofocus
            placeholder="粘贴 API_TOKEN"
            class="field w-full"
            @keyup.enter="submit"
          />
        </div>
        <div class="flex items-center justify-end gap-2 border-t border-base-600/60 px-4 py-2.5">
          <button class="btn btn-primary" :disabled="!input.trim()" @click="submit">保存并重新加载</button>
        </div>
      </div>
    </div>
  </Teleport>
</template>
