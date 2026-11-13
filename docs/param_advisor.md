# 每日参数体检 / 调参候选清单（自动生成，请勿手改）

> 生成时间：2026-11-13 16:00 ｜ 窗口 21 交易日（#70 §6 口径）
>
> **本清单为诊断信号，不是定值建议**：不自动改 `.env`、不基于单日下结论。任何参数变更须跑配对回测验证（门槛 >3.5pp / 21 天）。防止「今天亏了就改参数」的过拟合。
> 弱市专项（`paper_weak_exposure` / `paper_regime_cap_*`）需择时快照表，当前库无该表，仅列当前值不触发。

## 一、当前参数快照

| 参数 | 展示名 | 当前值 |
| --- | --- | --- |
| `paper_single_position_max` | 单票上限比例 | 0.2 |
| `paper_rotation_enabled` | 轮动开关 | False |
| `paper_rotation_min_score` | 轮动最低分 | 40.0 |
| `paper_rotation_gap` | 轮动分差 | 20.0 |
| `paper_rotation_max_per_day` | 每日轮动上限 | 10 |
| `paper_regime_ma` | 择时MA周期 | 60 |
| `paper_regime_weak_pause` | 弱市暂停买入 | True |
| `paper_weak_exposure` | 弱市仓位 | 0.3 |
| `paper_dynamic_exposure` | 动态仓位 | False |
| `paper_regime_cap_calm` | 平稳仓上限 | 0.0 |
| `paper_regime_cap_weak` | 弱势仓上限 | 0.3 |
| `paper_regime_cap_shock` | 震荡仓上限 | 0.4 |
| `paper_regime_cap_crisis` | 危机仓上限 | 0.15 |
| `paper_circuit_breaker_enabled` | 熔断开关 | False |
| `paper_circuit_breaker_dd` | 熔断回撤阈值 | 0.08 |
| `paper_circuit_breaker_target` | 熔断目标仓位 | 0.0 |
| `paper_t_down_add` | 做T浅加仓阈值 | 0.03 |
| `paper_t_down_full` | 做T满加仓阈值 | 0.06 |

## 二、诊断指标

| 指标 | 当前值 | 阈值 | 信号 |
| --- | --- | --- | --- |
| 滚动最大回撤 | +2.64% | ⚠️>8% 🔴>15% | ✅ |
| 滚动年化波动 | 9.78% | ⚠️>25% 🔴>35% | ✅ |
| 窗口累计收益 | -2.52% | ⚠️<-5% | ✅ |
| 「做T加仓」标签票胜率 | 0%（0/1） | 需≥5（当前1） | ✅ |
| 「轮动替换」换入票胜率 | 无样本 | — | ✅ |

## 三、调参候选清单（⚠️/🔴 触发项）

> 当前无触发项（全部 ✅ 或样本不足）。维持现状，无需调参。

---
