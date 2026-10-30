/**
 * 设计令牌。
 *
 * 配色走「中性深色 + 单一强调色」，参考成熟方案（shadcn/ui 的 dark 中性灰阶、
 * Tremor / Vercel Dashboard 的表面分层）：背景→卡片→悬浮三级只差明度，
 * 不靠蓝紫光晕营造层次，数据色（红涨绿跌）才抢眼。字体 Inter（拉丁）+ Noto Sans SC
 * （中文）+ JetBrains Mono（数字等宽，避免表格抖动）。
 *
 * 改色值即全局生效：模板里只写 `bg-base-800` / `text-content-muted` 这类语义类，
 * 图表色值集中在 `src/lib/theme.ts`，两处一起改才不会出现「页面变了图表没变」。
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 表面分层：900 页面底 / 800 卡片 / 700 悬浮与输入框 / 600 边框 / 500 强调边框
        base: {
          900: "#0A0C10",
          800: "#101319",
          700: "#171B23",
          600: "#252B37",
          500: "#333A49",
        },
        primary: {
          DEFAULT: "#3B82F6",
          light: "#93C5FD",
          dark: "#1D4ED8",
        },
        content: {
          DEFAULT: "#E8EBF1", // 主文字
          muted: "#98A2B3", // 次文字
          faint: "#697386", // 弱文字 / 说明
        },
        // A股习惯：红涨绿跌
        up: "#F04452",
        down: "#12B76A",
        warn: "#F5A524",
        accent: "#8B5CF6",
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
        // 规范：展示 26/600、标题 20/600、副标题 15/500、正文 13/400、标签 12、注 11
        display: ["26px", { lineHeight: "32px", fontWeight: "600", letterSpacing: "-0.01em" }],
        heading: ["20px", { lineHeight: "28px", fontWeight: "600", letterSpacing: "-0.01em" }],
        subheading: ["15px", { lineHeight: "22px", fontWeight: "500" }],
        body: ["13px", { lineHeight: "20px", fontWeight: "400" }],
        label: ["12px", { lineHeight: "18px", fontWeight: "500" }],
        caption: ["11px", { lineHeight: "16px", fontWeight: "400" }],
      },
      boxShadow: {
        // 层次靠阴影与边框，不靠发光
        glow: "0 0 0 1px rgba(59,130,246,0.28), 0 10px 30px -14px rgba(59,130,246,0.55)",
        card: "0 1px 2px rgba(0,0,0,0.32), 0 10px 26px -18px rgba(0,0,0,0.65)",
        pop: "0 18px 44px -14px rgba(0,0,0,0.7)",
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
