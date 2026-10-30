/**
 * 图表配色与轴样式**单一来源**。
 *
 * ECharts 不认 Tailwind 类，只能吃色值字符串。色值不在这里写死，而是运行时从
 * `src/style.css` 的 `--c-*` 变量读（与 Tailwind 同一套令牌），所以**切主题时
 * 图表跟着变**：调用方在 `computed` / 渲染函数里调用 `chartTheme()`，函数内部
 * 读取 `themeStore.current` 建立响应式依赖，主题一变就会重算。
 *
 * 用法：不要导出常量，按需调用。
 *   const option = computed(() => {
 *     const c = chartTheme()
 *     return { tooltip: { ...tooltipStyle(c) }, series: [{ lineStyle: { color: c.primaryLight } }] }
 *   })
 */
import { useThemeStore } from '../stores/theme'

export interface ChartPalette {
  up: string
  down: string
  neutral: string
  primary: string
  primaryLight: string
  warn: string
  accent: string
  text: string
  textMuted: string
  textFaint: string
  tooltipBg: string
  tooltipBorder: string
  axisLine: string
  splitLine: string
  /** 当前主题是否深色（tooltip 阴影等细节按它微调） */
  dark: boolean
}

type RGB = [number, number, number]

/** 读一个主题变量（RGB 三元组），读不到时退回默认值 */
function triplet(name: string, fallback: string): RGB {
  let raw = ''
  try {
    raw = getComputedStyle(document.documentElement).getPropertyValue(`--c-${name}`).trim()
  } catch {
    raw = ''
  }
  const parts = (raw || fallback).split(/[\s,]+/).map(Number)
  return [parts[0] || 0, parts[1] || 0, parts[2] || 0]
}

function hex(rgb: RGB): string {
  return '#' + rgb.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('')
}

function rgba(rgb: RGB, alpha: number): string {
  return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${alpha})`
}

/**
 * 当前主题下的图表色板。
 * 末尾的 `store.isDark` 不只是取色：它让调用方（computed / 模板渲染）登记对
 * 主题的依赖，切主题时图表自动重算。
 */
export function chartTheme(): ChartPalette {
  const store = useThemeStore()
  const dark = store.isDark

  const base600 = triplet('base-600', dark ? '37 43 55' : '222 226 233')
  const base800 = triplet('base-800', dark ? '16 19 25' : '255 255 255')

  return {
    // 数据色
    up: hex(triplet('up', '240 68 82')),
    down: hex(triplet('down', '18 183 106')),
    // 中性 / 强调
    neutral: hex(triplet('content-muted', '152 162 179')),
    primary: hex(triplet('primary', '59 130 246')),
    primaryLight: hex(triplet('primary-light', '147 197 253')),
    warn: hex(triplet('warn', '245 165 36')),
    accent: hex(triplet('accent', '139 92 246')),
    // 文字
    text: hex(triplet('content', '232 235 241')),
    textMuted: hex(triplet('content-muted', '152 162 179')),
    textFaint: hex(triplet('content-faint', '105 115 134')),
    // 容器与轴
    tooltipBg: rgba(base800, 0.97),
    tooltipBorder: hex(base600),
    axisLine: hex(base600),
    splitLine: rgba(base600, 0.55),
    dark,
  }
}

/** 统一的 tooltip 外观：主题底色 + 1px 边框 + 圆角 */
export function tooltipStyle(c: ChartPalette) {
  return {
    backgroundColor: c.tooltipBg,
    borderColor: c.tooltipBorder,
    borderWidth: 1,
    padding: [8, 10] as [number, number],
    textStyle: {
      color: c.text,
      fontSize: 12,
      fontFamily: 'Inter, "Noto Sans SC", system-ui, sans-serif',
    },
    extraCssText: c.dark
      ? 'border-radius:8px;box-shadow:0 12px 32px -12px rgba(0,0,0,.7);'
      : 'border-radius:8px;box-shadow:0 12px 28px -14px rgba(16,24,40,.28);',
  }
}

/** 轴刻度文字 */
export function axisLabel(c: ChartPalette, muted = false) {
  return { color: muted ? c.textMuted : c.textFaint, fontSize: 11 }
}
