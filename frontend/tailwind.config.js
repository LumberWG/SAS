/**
 * 设计令牌。
 *
 * 配色全部走 CSS 变量（`--c-*`，RGB 三元组），主题在 `src/style.css` 里按
 * `[data-theme=...]` 定义：石墨黑 / 深海蓝 / 象牙白 / 暖沙。这样 `bg-base-800`、
 * `text-content-muted` 这些语义类在**换主题时自动跟着变**，模板里不出现任何
 * 色值字面量；图表另由 `src/lib/chartTheme.ts` 读同一批变量。
 *
 * 字号：2026-10 整体上调一档（正文 14px 起），并按「正文/标签/注」三档收敛，
 * 避免满屏 11px 小字。
 *
 * @type {import('tailwindcss').Config}
 */

/** 把 `--c-xxx` 三元组接进 Tailwind 颜色（保留 /opacity 支持） */
const token = (name) => `rgb(var(--c-${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 表面分层：900 页面底 / 800 卡片 / 700 悬浮与输入 / 600 边框 / 500 强调边框
        base: {
          900: token("base-900"),
          800: token("base-800"),
          700: token("base-700"),
          600: token("base-600"),
          500: token("base-500"),
        },
        primary: {
          DEFAULT: token("primary"),
          light: token("primary-light"),
          dark: token("primary-dark"),
        },
        content: {
          DEFAULT: token("content"),
          muted: token("content-muted"),
          faint: token("content-faint"),
        },
        // A股习惯：红涨绿跌
        up: token("up"),
        down: token("down"),
        warn: token("warn"),
        accent: token("accent"),
      },
      fontFamily: {
        sans: [
          "Inter",
          "Noto Sans SC",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "system-ui",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "SFMono-Regular", "Consolas", "monospace"],
      },
      fontSize: {
        // 展示 28 / 标题 22 / 副标题 16 / 正文 14 / 标签 13 / 注 12
        display: ["28px", { lineHeight: "36px", fontWeight: "600", letterSpacing: "-0.01em" }],
        heading: ["22px", { lineHeight: "30px", fontWeight: "600", letterSpacing: "-0.01em" }],
        subheading: ["16px", { lineHeight: "24px", fontWeight: "500" }],
        body: ["14px", { lineHeight: "22px", fontWeight: "400" }],
        label: ["13px", { lineHeight: "20px", fontWeight: "500" }],
        caption: ["12px", { lineHeight: "18px", fontWeight: "400" }],
      },
      boxShadow: {
        // 变量化：浅色主题阴影要更淡，交给 style.css 里的主题块覆盖
        glow: "var(--shadow-glow)",
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
      },
      transitionTimingFunction: {
        panel: "cubic-bezier(0.22, 1, 0.36, 1)",
      },
      animation: {
        "pulse-slow": "pulse 2.5s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "fade-in": "fadeIn 0.35s cubic-bezier(0.22, 1, 0.36, 1)",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
