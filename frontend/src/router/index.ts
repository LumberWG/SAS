/**
 * 路由定义：按「自动交易五段式」组织页面（见 docs/sas_formulas.md §8）。
 *
 *   ① 一选 · 股票池   /screening   全市场扫描 → 候选池
 *   ② 二选 · 自选股   /signals     池内打分排序 → topN 关注
 *   ③ 三选 · 交易队列 /advisory    详细分析 → topM 候选
 *   ④ 交易池 · 持仓   /paper       判定规则（模式/队列/候补）+ 账户、持仓、流水
 *                                  （同一页两个 tab；旧的 /paper-pool 会跳到这里）
 *
 * 概览与回测为辅助页，不属于流水线环节。
 */
import { createRouter, createWebHistory, type RouteLocation } from 'vue-router'

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
        meta: { title: '① 一选 · 股票池', stage: 1, icon: 'Filter' },
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
        meta: { title: '② 二选 · 自选股', stage: 2, icon: 'Bell' },
      },
      {
        path: 'advisory',
        name: 'advisory',
        component: () => import('../views/Advisory.vue'),
        meta: { title: '③ 三选 · 交易队列', stage: 3, icon: 'Lightbulb' },
      },
      {
        // ④ 交易池 · 持仓：同一个组件、同一份数据，页内用 tab 切换「判定与队列 / 账户复盘」。
        // 拆成两个菜单项（④交易池 + ⑤持仓与成交）只是让人来回点，已合并。
        path: 'paper',
        name: 'paper',
        component: () => import('../views/PaperTrading.vue'),
        meta: { title: '④ 交易池 · 持仓', stage: 4, icon: 'Wallet' },
      },
      {
        // 兼容旧书签与旧文档链接：/paper-pool 直接落到合并页的交易池 tab
        path: 'paper-pool',
        redirect: (to: RouteLocation) => ({ path: '/paper', query: { ...to.query, tab: 'pool' } }),
      },
      {
        // 系统设置：只读总览「当前生效值 + 来源 + 是否要重启」，不做写操作
        path: 'settings',
        name: 'settings',
        component: () => import('../views/Settings.vue'),
        meta: { title: '系统设置', icon: 'Settings' },
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
