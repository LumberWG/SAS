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
})

export default router
