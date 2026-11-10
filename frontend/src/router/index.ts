/**
 * 路由定义：按职能划分的信息架构（工作台 / 投研分析 / 交易执行 / 系统管理）。
 *
 * 业务内核仍是「自动交易五段式」（见 docs/sas_formulas.md §8），但菜单不再用
 * ①②③ +「一选/二选/三选」这种内部流程口语，改为金融终端通行的职能命名：
 *
 *   工作台      市场总览       /dashboard
 *   投研分析    全市场选股     /screening   全市场扫描 → 候选池
 *              信号与自选     /signals     池内打分排序 → topN 关注
 *              决策建议       /advisory    详细分析 → topM 候选
 *              策略回测       /backtest    参数与历史绩效验证（研究链路收口）
 *   交易执行    模拟交易       /paper       判定规则 + 账户、持仓、流水（页内两 tab）
 *   系统管理    运行监控       /ops
 *              系统设置       /settings
 *
 * 注：后端 API、策略文档与账本里的「一选/二选/三选」是业务术语，不随菜单改名。
 * meta.stage 保留五段式的阶段序号，供以后做流程可视化/引导使用（当前未消费）。
 */
import { createRouter, createWebHistory, type RouteLocation } from 'vue-router'
import { startProgress, doneProgress } from '../composables/navProgress'

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
        meta: { title: '市场总览', icon: 'LayoutDashboard' },
      },
      {
        path: 'stock/:code',
        name: 'stock-detail',
        component: () => import('../views/StockDetail.vue'),
        meta: { title: '个股详情', icon: 'LineChart' },
      },
      {
        path: 'screening',
        name: 'screening',
        component: () => import('../views/Screening.vue'),
        meta: { title: '全市场选股', stage: 1, icon: 'ScanSearch' },
      },
      {
        path: 'signals',
        name: 'signals',
        component: () => import('../views/Signals.vue'),
        meta: { title: '信号与自选', stage: 2, icon: 'Radar' },
      },
      {
        path: 'advisory',
        name: 'advisory',
        component: () => import('../views/Advisory.vue'),
        meta: { title: '决策建议', stage: 3, icon: 'Target' },
      },
      {
        path: 'backtest',
        name: 'backtest',
        component: () => import('../views/Backtest.vue'),
        meta: { title: '策略回测', icon: 'FlaskConical' },
      },
      {
        // 模拟交易：判定与队列 / 账户复盘是同一份数据的两种视角，页内用 tab 切换。
        path: 'paper',
        name: 'paper',
        component: () => import('../views/PaperTrading.vue'),
        meta: { title: '模拟交易', stage: 4, icon: 'Wallet' },
      },
      {
        // 兼容旧书签与旧文档链接：/paper-pool 直接落到合并页的交易池 tab
        path: 'paper-pool',
        redirect: (to: RouteLocation) => ({ path: '/paper', query: { ...to.query, tab: 'pool' } }),
      },
      {
        // 运行监控：只读展示调度任务、数据新鲜度、访客访问记录
        path: 'ops',
        name: 'ops',
        component: () => import('../views/Ops.vue'),
        meta: { title: '运行监控', icon: 'Server' },
      },
      {
        // 系统设置：参数当前生效值 + 来源 + 是否要重启
        path: 'settings',
        name: 'settings',
        component: () => import('../views/Settings.vue'),
        meta: { title: '系统设置', icon: 'SlidersHorizontal' },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 路由切换进度条：首次进入需下载动态 chunk（含 echarts 706KB）时给出顶部细条反馈，
// keep-alive 命中的页面解析近乎同步，条一闪而过甚至不显，无打扰。
router.beforeEach(() => {
  startProgress()
})

router.afterEach((to) => {
  doneProgress()
  const title = to.meta.title as string | undefined
  document.title = title ? `${title} · SAS 智能投研终端` : 'SAS 智能投研终端'
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
