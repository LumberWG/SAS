/**
 * 路由导航进度条（极轻量，不依赖第三方库）。
 *
 * 用途：路由切换、尤其是「首次进入某视图需要下载动态 chunk（含 echarts 706KB）」时，
 * 给一个顶部细进度条反馈，消除「点了没反应 / 卡一下」的体感。
 *
 * 注意：Vue Router 的 afterEach 在**异步组件解析完成后**才触发，所以：
 *   - 首次进入新页面（需下载 chunk）：beforeEach 启动 → chunk 下载期间条在动 → afterEach 收尾；
 *   - keep-alive 命中的页面（组件已缓存）：解析近乎同步，条一闪而过甚至不显，无打扰。
 */
import { reactive } from 'vue'

export const navProgress = reactive({
  active: false,
  value: 0,
})

let timer: ReturnType<typeof setInterval> | undefined

export function startProgress() {
  if (timer) clearInterval(timer)
  navProgress.active = true
  navProgress.value = 12
  // 渐进到 90 表示「仍在加载」，给用户明确的进行中反馈
  timer = setInterval(() => {
    if (navProgress.value < 90) {
      navProgress.value += (90 - navProgress.value) * 0.12
    }
  }, 200)
}

export function doneProgress() {
  if (timer) {
    clearInterval(timer)
    timer = undefined
  }
  navProgress.value = 100
  // 收尾动画后再隐藏，避免「跳变」
  setTimeout(() => {
    navProgress.active = false
    navProgress.value = 0
  }, 280)
}
