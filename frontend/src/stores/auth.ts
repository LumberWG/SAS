/**
 * 访问令牌 store。
 *
 * 后端 `.env` 里配了 `API_TOKEN` 时，所有 `/api/*` 请求都要带 `X-API-Token`。
 * 令牌存在**本机浏览器**的 localStorage（`sas-api-token`），换台电脑要重新输入 ——
 * 它属于部署凭据，不该写进代码或配置文件随仓库分发。
 *
 * 触发方式：任意请求收到 401 → `markNeeded()` → 挂载在根组件的 `TokenGate` 弹出输入框。
 */
import { defineStore } from 'pinia'

const STORAGE_KEY = 'sas-api-token'

function readToken(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: readToken(),
    /** 是否正在要求用户输入令牌（收到 401 时置真） */
    needed: false,
  }),
  actions: {
    setToken(value: string) {
      this.token = value.trim()
      this.needed = false
      try {
        localStorage.setItem(STORAGE_KEY, this.token)
      } catch {
        /* 隐私模式存不了：本次会话仍可用（内存里有） */
      }
    },
    markNeeded() {
      this.needed = true
    },
    clear() {
      this.token = ''
      this.needed = false
      try {
        localStorage.removeItem(STORAGE_KEY)
      } catch {
        /* ignore */
      }
    },
  },
})
