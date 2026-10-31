import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    allowedHosts: true,
    port: 5173,
    // 开发代理：/api → FastAPI 后端（uvicorn 默认 8000 端口）
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // 分包：echarts 全家（core/charts/components/renderers + vue-echarts）单独成块。
    // 不拆的话 Rollup 会把它和 chartTheme 合成一个 500KB 的巨块，首屏路由
    // （/dashboard 直接画图）必须先下载解析完才能出第一帧；
    // 拆开后可被浏览器长缓存，且懒加载路由进入时再取。
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: [
            'echarts/core',
            'echarts/charts',
            'echarts/components',
            'echarts/renderers',
            'vue-echarts',
          ],
        },
      },
    },
    chunkSizeWarningLimit: 900,
  },
})
