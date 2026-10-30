/**
 * 图表配色与轴样式**单一来源**。
 *
 * ECharts 不认 Tailwind 类，只能吃色值字符串；以前每个图表文件各写一份
 * `#111A2E` / `#38BDF8`，改主题时页面变了图表没变，一眼就能看出割裂。
 * 现在色值只在 `tailwind.config.js` 与这里各一份，两边同步即可（注释里标注对应 token）。
 */
export const chart = {
  // 数据色（对应 tailwind up / down）
  up: '#F04452',
  down: '#12B76A',
  // 中性/强调（对应 content-muted / primary / primary-light / warn / accent）
  neutral: '#98A2B3',
  primary: '#3B82F6',
  primaryLight: '#93C5FD',
  warn: '#F5A524',
  accent: '#8B5CF6',
  // 表面与文字（对应 base-800 / base-600 / content / content-muted / content-faint）
  tooltipBg: 'rgba(16, 19, 25, 0.96)',
  tooltipBorder: '#252B37',
  text: '#E8EBF1',
  textMuted: '#98A2B3',
  textFaint: '#697386',
  axisLine: '#252B37',
  splitLine: 'rgba(37, 43, 55, 0.55)',
} as const

/** 统一的 tooltip 外观：深色玻璃底 + 1px 边框 + 圆角 */
export const tooltipStyle = {
  backgroundColor: chart.tooltipBg,
  borderColor: chart.tooltipBorder,
  borderWidth: 1,
  padding: [8, 10] as [number, number],
  textStyle: {
    color: chart.text,
    fontSize: 12,
    fontFamily: 'Inter, "Noto Sans SC", system-ui, sans-serif',
  },
  extraCssText: 'border-radius:8px;box-shadow:0 12px 32px -12px rgba(0,0,0,.7);',
}

/** 轴刻度文字（弱） */
export const axisLabel = { color: chart.textFaint, fontSize: 10 }
/** 轴刻度文字（次强，用于类目轴） */
export const axisLabelMuted = { color: chart.textMuted, fontSize: 10 }
/** 轴线 */
export const axisLine = { lineStyle: { color: chart.axisLine } }
/** 网格虚线 */
export const splitLine = { lineStyle: { color: chart.splitLine, type: 'dashed' as const } }
