/**
 * 全局通知 store：统一承载 Toast 提示与「数据源降级」状态。
 *
 * 后端在数据源故障时会返回 502（异常）/ 503（超时降级），前端通过
 * axios 拦截器自动调用 markDataSourceDown()，避免每个页面各自写错误提示。
 */
import { defineStore } from 'pinia'

export type ToastType = 'info' | 'warn' | 'error' | 'success'

export interface ToastItem {
  id: number
  type: ToastType
  message: string
}

let seq = 0

export const useNotifyStore = defineStore('notify', {
  state: () => ({
    toasts: [] as ToastItem[],
    // 数据源降级状态：避免短时间内的重复提示
    dataSourceDown: false,
  }),
  actions: {
    push(message: string, type: ToastType = 'info', ttl = 4000): number {
      const id = ++seq
      this.toasts.push({ id, type, message })
      if (ttl > 0) {
        window.setTimeout(() => this.dismiss(id), ttl)
      }
      return id
    },
    dismiss(id: number) {
      this.toasts = this.toasts.filter((t) => t.id !== id)
    },
    /** 行情数据源不可用：仅提示一次。 */
    markDataSourceDown() {
      if (this.dataSourceDown) return
      this.dataSourceDown = true
      this.push(
        '行情数据源暂时不可用，部分实时行情可能延迟或缺失（已尽量使用本地缓存）',
        'warn',
        6000,
      )
    },
    /** 数据源恢复：清除降级态并提示。 */
    clearDataSourceDown() {
      if (!this.dataSourceDown) return
      this.dataSourceDown = false
      this.push('行情数据源已恢复', 'success', 3000)
    },
  },
})
