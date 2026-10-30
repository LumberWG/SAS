/**
 * 主题 store：石墨黑 / 深海蓝 / 象牙白 / 暖沙。
 *
 * 只改 `<html data-theme="...">`，颜色全部由 `src/style.css` 里的 CSS 变量提供，
 * 所以切主题不需要重渲染组件树，`bg-base-800` 这类语义类自动跟着变；
 * 图表（ECharts 吃不到 CSS 类）由 `src/lib/chartTheme.ts` 读取同一批变量后重算。
 *
 * 选择存在 localStorage（跟浏览器走，不进后端配置）——它是显示偏好，不是业务参数，
 * 换台电脑看到默认主题是预期行为。
 */
import { defineStore } from 'pinia'

export type ThemeKey = 'dark' | 'midnight' | 'light' | 'sand'

export interface ThemeMeta {
  key: ThemeKey
  /** 展示名 */
  name: string
  /** 一句话说明，用于设置页卡片 */
  desc: string
  /** 是否深色（图表 tooltip 阴影等细节按这个微调） */
  dark: boolean
  /** 预览色块：页面底 / 卡片 / 主色 / 文字 */
  swatch: [string, string, string, string]
}

export const THEMES: ThemeMeta[] = [
  {
    key: 'dark',
    name: '石墨黑',
    desc: '默认深色，长时间盯盘不刺眼',
    dark: true,
    swatch: ['#0A0C10', '#101319', '#3B82F6', '#E8EBF1'],
  },
  {
    key: 'midnight',
    name: '深海蓝',
    desc: '偏冷的深蓝底，接近行情终端',
    dark: true,
    swatch: ['#080C18', '#0D1322', '#4C8DFF', '#E2E9F5'],
  },
  {
    key: 'light',
    name: '象牙白',
    desc: '浅色，适合办公环境与投屏演示',
    dark: false,
    swatch: ['#F5F6F9', '#FFFFFF', '#2563EB', '#181D26'],
  },
  {
    key: 'sand',
    name: '暖沙',
    desc: '低饱和米色，纸感阅读更放松',
    dark: false,
    swatch: ['#F9F7F3', '#FFFDFA', '#2563EB', '#231F1A'],
  },
]

const STORAGE_KEY = 'sas-theme'

function metaOf(key: ThemeKey): ThemeMeta {
  return THEMES.find((t) => t.key === key) ?? THEMES[0]
}

function readStored(): ThemeKey {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved && THEMES.some((t) => t.key === saved)) return saved as ThemeKey
  } catch {
    /* 隐私模式下 localStorage 不可用：用默认主题 */
  }
  // 默认给浅色：白天办公/投屏更常看，深色是主动选择
  return 'light'
}

export const useThemeStore = defineStore('theme', {
  state: () => ({
    current: readStored(),
  }),
  getters: {
    meta: (s): ThemeMeta => metaOf(s.current),
    isDark: (s): boolean => metaOf(s.current).dark,
  },
  actions: {
    /** 把当前主题写到 <html> 上（启动时与切换时各调一次） */
    apply() {
      const meta = metaOf(this.current)
      document.documentElement.dataset.theme = this.current
      // 移动端/浏览器地址栏跟着底色走
      document
        .querySelector('meta[name="theme-color"]')
        ?.setAttribute('content', meta.swatch[0])
    },
    set(key: ThemeKey) {
      this.current = key
      try {
        localStorage.setItem(STORAGE_KEY, key)
      } catch {
        /* 存不了就算了，本次会话仍然生效 */
      }
      this.apply()
    },
  },
})
