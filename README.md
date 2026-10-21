# SAS — A股智能分析系统

面向国内 A 股（沪深京）的个人学习研究工具：实时行情监控、技术指标策略全市场选股、历史回测，并可选接入大模型生成结构化买卖建议。

> **免责声明：本系统仅供学习研究使用，所有数据与结论不构成任何投资建议。股市有风险，投资需谨慎。**

## 功能总览

| 模块 | 能力 |
| --- | --- |
| 实时行情 | 三大指数概览、涨跌家数、成交额统计、市场情绪分级（5 档权重）、自选股盯盘 |
| 策略选股 | 6 种内置经典策略 + 自定义策略，全市场 5000+ 股票并发扫描，命中信号自动持久化 |
| 策略编辑 | 所有策略参数前端可调，支持另存为自定义策略（持久化，重启自动加载） |
| LLM 智能分析 | 接入任意 OpenAI 兼容大模型，生成决策仪表盘（结论 + 入场价/止损价/目标价 + 六维检查清单） |
| 历史回测 | 信号驱动回放，输出收益率/胜率/最大回撤/夏普与净值曲线 |
| 定时监控 | 盘中快照刷新、异动检测（涨跌停/放量突破/急涨急跌）、收盘后自动选股推送 |

## 市场情绪分级（5 档）

市场整体情绪量化为 5 档离散等级，每档有默认权重（-1 ~ +1），为选股信号与 LLM 分析提供「市场环境」上下文。

| 情绪等级 | 温度区间 | 权重 | 操作建议 |
| --- | --- | --- | --- |
| 极度亢奋 | ≥ 80 | **+1** | 赚钱效应极致，注意波动加剧，追高需谨慎 |
| 多头占优 | 60–80 | **+0.5** | 顺势环境，趋势突破类策略有效性较高 |
| 中性 | 40–60 | **0** | 结构性机会为主，严选个股、控制仓位 |
| 空头占优 | 20–40 | **-0.5** | 防守为主，降低盈利预期，回避弱势股 |
| 极度悲观 | < 20 | **-1** | 恐慌区域，可关注超跌反弹机会或观望 |

**映射依据**：市场温度计（0-100）= 上涨占比 × 60 + 涨停占比（封顶 5%）× 40，按等宽阈值 [80, 60, 40, 20] 划分 5 档。阈值与权重定义见 `backend/app/market/sentiment.py`（调整需同步本文档与 `tests/test_sentiment.py`）。

**权重的三层用途**：
1. **展示**：Dashboard「市场情绪」卡片显示等级徽标 + 权重 + 操作建议
2. **信号**：选股结果记录扫描开始时的情绪快照；LLM 决策仪表盘在情绪权重 ≤ -0.5 时把规则结论「买入」自动降级为「持有」
3. **LLM 上下文**：个股分析时注入当前情绪等级与权重，AI 结论结合市场环境（如亢奋期提示追高风险、悲观期提示逆环境信号）

## 技术栈

- **后端**：Python 3.11+ / FastAPI / Pandas / akshare / SQLite + Parquet / APScheduler
- **前端**：Vue 3 + Vite + TypeScript + TailwindCSS + ECharts + Pinia
- **LLM**：OpenAI 兼容协议（DeepSeek / 通义 / 智谱 / OpenAI 等可配置），**未配置 API Key 时自动降级为技术面规则评分，其余功能完整可用**

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│ Vue3 前端（5 页面）  仪表盘 / 选股 / 个股 / 回测 / 信号自选   │
└──────────────────────────┬──────────────────────────────────┘
                           │ REST (/api)
┌──────────────────────────▼──────────────────────────────────┐
│ FastAPI 后端                                                 │
│  market · screening · backtest · analysis · watchlist ·       │
│  monitor · strategies（策略管理）                             │
│  ┌──────────┐  ┌──────────┐  ┌────────┐  ┌──────┐            │
│  │ 扫描器    │  │ 回测引擎 │  │ LLM    │  │ 调度 │            │
│  │ asyncio  │  │ 向量化   │  │ 分析器 │  │  APS │            │
│  └────┬─────┘  └────┬─────┘  └───┬────┘  └──┬───┘            │
│  ┌────▼─────────────▼────────────▼─────────▼───┐            │
│  │ 指标引擎(自研) + 策略注册表(工厂+参数化)      │            │
│  └────────────────────┬────────────────────────┘            │
│  ┌────────────────────▼────────────────────────┐            │
│  │ 数据源抽象层（可插拔降级链）                  │            │
│  │  东方财富 →(不可达) 腾讯 →(风控501) 新浪     │            │
│  │  + 接口熔断器 + 域名级节流 + 失败重试         │            │
│  └────────────────────┬────────────────────────┘            │
│  ┌────────────────────▼────────────────────────┐            │
│  │ 存储：Parquet K线缓存（按代码增量）+ SQLite   │            │
│  │ （自选股/信号/回测/自定义策略）              │            │
│  └─────────────────────────────────────────────┘            │
└─────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/Mac

pip install -r requirements.txt

# 可选：复制 env 样例并按需修改（LLM / Webhook / 调度开关）
copy .env.example .env          # Windows
# cp .env.example .env          # Linux/Mac

# 启动（默认 8000 端口，启动时自动预热股票池、加载自定义策略并开启调度器）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后访问交互式文档：http://127.0.0.1:8000/docs

### 2. 前端

```bash
cd frontend
npm install
npm run dev        # 开发服务器，默认 http://localhost:5173
# npm run build    # 生产构建
```

前端开发服务器已配置 `/api` 代理到 `http://127.0.0.1:8000`，无需额外配置。

### 3. 配置 LLM（可选）

编辑 `backend/.env`：

```ini
LLM_ENABLED=true
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxxxxxx
LLM_MODEL=deepseek-chat
```

任意 OpenAI 兼容服务均可（DeepSeek / 通义千问 / 智谱 / OpenAI / Gemini）。

### 4. 配置通知（可选）

```ini
NOTIFY_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
```

支持企业微信 / 飞书 / 钉钉机器人；未配置时仅控制台输出。

## 内置策略与策略编辑

| 策略 | 核心逻辑 |
| --- | --- |
| 海龟突破 | 收盘价创 N 日新高 + 成交额过阈 + 阳线（周期/阈值可调） |
| 均线放量 | MA5>MA10>MA20 多头排列 + 放量突破 + 站上 5 日线 |
| MACD金叉 | DIF 上穿 DEA 且 MACD 柱 > 0（可过滤零轴位置） |
| KDJ超卖金叉 | J < 阈值 超卖区 + K 上穿 D（阈值可调） |
| 平台突破 | N 日窄幅横盘 + 放量突破平台上沿 |
| 动量突破 | N 日涨幅 ≥ 阈值 + 贴近新高（欧奈尔 RPS 思想） |

**策略可编辑**：在「选股中心」点击策略卡片的 ✏️ 按钮打开参数编辑器
（数字输入+滑块 / 开关 / 下拉三类控件），调整后可**另存为自定义策略**。
自定义策略持久化在 SQLite（`custom_strategies` 表），重启自动加载，
可像内置策略一样用于选股扫描与回测，也可随时删除；
内置策略不可修改、不可删除。

新增内置策略只需继承 `Strategy` 实现 `evaluate` 并声明 `parameters`
（见 `app/strategies/builtin.py`）。

## 数据源说明（重要）

本项目面向**免费数据源**，针对国内网络环境实现了三级降级链：

```
东方财富(akshare) → 腾讯 → 新浪
```

- **熔断器**：某接口连续失败 3 次后进入 5 分钟冷却期，冷却期内直接降级，避免每只股票浪费重试时间
- **域名级节流**：同一域名请求间隔 ≥ 0.15s，降低触发风控（返回 501 JS 挑战页）的概率
- **新浪兜底**：该接口历史悠久、风控宽松，是腾讯被限流后的最终保障

实测注意：
- K线成交量已统一换算为「手」；成交额为估算值（腾讯/新浪源不返回真实成交额）
- 交易日判断不含法定节假日（个人使用场景可接受）

## 测试

```bash
cd backend
.venv\Scripts\python.exe -m pytest tests/ -q
```

66 个单元测试，覆盖：指标公式正确性、各策略命中/不命中场景、策略参数注入与
自定义策略全链路（注册/持久化/启动加载）、回测止盈/止损/绩效指标、市场情绪
分级（阈值边界/权重/快照计算/逆环境降级）、交易时段判断、异动检测与去重、
通知管道。

## 目录结构

```
SAS/
├── backend/
│   ├── app/
│   │   ├── main.py            # FastAPI 入口、lifespan（预热+自定义策略加载+调度器）
│   │   ├── config.py          # Pydantic 配置（.env 可覆盖）
│   │   ├── api/               # market/screening/backtest/analysis/watchlist/monitor/strategies
│   │   ├── data/              # 数据源抽象、akshare 实现、降级链、缓存
│   │   ├── indicators/        # 自研技术指标（含公式注释）
│   │   ├── strategies/        # 基类(参数化)/注册表/内置策略/自定义策略加载
│   │   ├── market/            # 市场情绪分级（5 档 + 权重）
│   │   ├── screener/          # 全市场并发扫描器（含扫描范围）
│   │   ├── backtest/          # 向量化回测引擎
│   │   ├── llm/               # OpenAI 兼容客户端 + 决策仪表盘
│   │   ├── monitor/           # APScheduler 调度器
│   │   └── notify/            # 通知管道（控制台/Webhook/邮件预留）
│   ├── tests/                 # 85 个单元测试
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── views/             # Dashboard/Screening/StockDetail/Backtest/Signals
│   │   ├── components/        # KLineChart/MetricCard/ConclusionBadge/StrategyPanel/StrategyEditor/ScanResultTable
│   │   ├── api/               # 接口封装
│   │   ├── layouts/           # 侧边导航 + 顶栏布局
│   │   ├── stores/            # Pinia 全局状态
│   │   └── router/
│   └── package.json
├── start_dev.py               # 一键启动前后端（含端口清理与健康检查）
└── data/                      # 运行时生成：klines/*.parquet + sas.db（不入库）
```

## 设计参考

本项目架构借鉴了以下成熟开源项目（均为 MIT 协议）：

- [Sequoia-X](https://github.com/silverhornz/Sequoia-X) — 收盘后自动选股流水线、策略工厂模式
- [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) — LLM 决策仪表盘结构化设计
- [tick-stock-panel](https://github.com/shy3130/tick-stock-panel) — 数据源可插拔能力路由
- [akshare](https://github.com/akfamily/akshare) — 免费 A 股数据源

---

**再次提醒：本系统仅供学习研究，不构成投资建议。**
