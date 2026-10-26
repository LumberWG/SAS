/**
 * 路由定义：5 个功能页面，统一挂在 DefaultLayout 布局下。
 */
import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    component: () => import('../layouts/DefaultLayout.vue'),
    redirect: '/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('../views/Dashboard.vue'),
        meta: { title: '大盘概览', icon: 'LayoutDashboard' },
      },
      {
        path: 'screening',
        name: 'screening',
        component: () => import('../views/Screening.vue'),
        meta: { title: '选股中心', icon: 'Filter' },
      },
      {
        path: 'stock/:code',
        name: 'stock-detail',
        component: () => import('../views/StockDetail.vue'),
        meta: { title: '个股详情', icon: 'LineChart' },
      },
      {
        path: 'backtest',
        name: 'backtest',
        component: () => import('../views/Backtest.vue'),
        meta: { title: '回测中心', icon: 'History' },
      },
      {
        path: 'signals',
        name: 'signals',
        component: () => import('../views/Signals.vue'),
        meta: { title: '信号与自选', icon: 'Bell' },
      },
      {
        path: 'advisory',
        name: 'advisory',
        component: () => import('../views/Advisory.vue'),
        meta: { title: '智能建议', icon: 'Lightbulb' },
      },
      {
        path: 'paper',
        name: 'paper',
        component: () => import('../views/PaperTrading.vue'),
        meta: { title: '模拟交易', icon: 'Wallet' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · SAS A股分析` : 'SAS A股分析'
  // 导航成功说明入口与懒加载 chunk 匹配，解除「已尝试重载」标记
  sessionStorage.removeItem('sas:chunk-reload')
})

// 重新部署后旧入口引用的 chunk hash 已不存在，动态 import 会失败（页面点不进去）。
// 自动整页刷新一次拿到新入口；只尝试一次，避免死循环。
router.onError((err, to) => {
  const message = String((err as Error)?.message ?? err)
  const chunkFailed = /Failed to fetch dynamically imported module|Importing a module script failed|error loading dynamically imported module/i.test(
    message
  )
  if (chunkFailed && !sessionStorage.getItem('sas:chunk-reload')) {
    sessionStorage.setItem('sas:chunk-reload', '1')
    window.location.assign(to?.fullPath || '/')
    return
  }
  console.error('[router]', err)
})

export default router
