/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{vue,js,ts,jsx,tsx}"],
  theme: {
    extend: {
      colors: {
        // 深色金融仪表盘色板
        base: {
          900: "#0B1220", // 页面背景
          800: "#111A2E", // 卡片背景
          700: "#1A2540", // 悬浮/ elevated 背景
          600: "#243354", // 边框/分隔
        },
        primary: {
          DEFAULT: "#2563EB",
          light: "#38BDF8",
          dark: "#1E3A8A",
        },
        content: {
          DEFAULT: "#E6EDF7", // 主文字
          muted: "#8B9BB4",    // 次文字
          faint: "#5C6B85",    // 弱文字
        },
        // A股习惯：红涨绿跌
        up: "#F5222D",
        down: "#00B96B",
        warn: "#FAAD14",
        accent: "#722ED1",
      },
      fontFamily: {
        sans: ["Noto Sans", "PingFang SC", "Microsoft YaHei", "sans-serif"],
        mono: ["JetBrains Mono", "Consolas", "monospace"],
      },
      fontSize: {
        // 规范：标题20/600、副标题15/500、正文13/400
        heading: ["20px", { lineHeight: "28px", fontWeight: "600" }],
        subheading: ["15px", { lineHeight: "22px", fontWeight: "500" }],
        body: ["13px", { lineHeight: "20px", fontWeight: "400" }],
      },
      boxShadow: {
        glow: "0 0 20px rgba(37, 99, 235, 0.25)",
        card: "0 4px 24px rgba(0, 0, 0, 0.35)",
      },
      animation: {
        "pulse-slow": "pulse 2.5s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "fade-in": "fadeIn 0.4s ease-out",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
