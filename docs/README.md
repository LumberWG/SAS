# 文档统一入口（docs/README.md）

> 本仓库文档已统一收拢：**参考/说明类文档都在 `docs/`**，根目录只保留 6 个「必须在根」的文件。
> 新增文档请放进 `docs/`（分析/历史报告放 `docs/analysis/`），不要再散落在根目录。

## 一、根目录保留（必须在根，勿移动）

| 文件 | 为什么必须在根 |
|---|---|
| `README.md` | GitHub 仓库首页、被全项目引用 |
| `AGENTS.md` | 各 AI 编码工具在仓库根自动读取的入场须知 |
| `CONTRIBUTING.md` | 开源贡献约定 |
| `LICENSE` | 授权声明（MIT） |
| `WORKLOG.md` | 策略变更记录；被大量代码注释与规则按「根目录文件名」引用（约定） |
| `BACKTEST_LOG.md` | 回测实验记录；同上（约定，编号唯一、只追加） |

## 二、`docs/` 核心文档

| 文件 | 内容 |
|---|---|
| `docs/overview.md` | **代码全景梳理**（分层架构 / 端到端主线 / 三套分数 / 三层配置 / 近期改动 / 排错）——先读这个 |
| `docs/flow.md` | 一次买卖的完整链路（选股→入队→撮合→五段式→卖出风控），最该通读 |
| `docs/sas_formulas.md` | 公式与实现口径（因子/风控/撮合/回测参数总表） |
| `docs/PROJECT_SUMMARY.md` | **前期工作四段式总结**（项目特点与同类对比 / 全流程文字+公式 / 已完成回测结论 / 后续改善方向），2026-11-12 |

## 三、`docs/` 自动生成（脚本产出，勿手改）

| 文件 | 产出脚本 |
|---|---|
| `docs/daily_trade_review.md` | `backend/scripts/daily_review.py`（收盘每日复盘，按日期追加） |
| `docs/param_advisor.md` | `backend/scripts/param_advisor.py`（参数体检） |
| `docs/position_pnl.md` | `backend/scripts/position_pnl.py`（持仓盈亏） |

## 四、`docs/analysis/`（研究/历史报告，原在根目录）

| 文件 | 内容 |
|---|---|
| `docs/analysis/ALPHA_CONCLUSIONS.md` | Alpha 主题的已定论/待验证/差异点三分表 |
| `docs/analysis/ALPHA_ENGINE_ROADMAP.md` | Alpha 工程路线图 |
| `docs/analysis/LITERATURE_CONSENSUS.md` | 文献共识汇编 |
| `docs/analysis/STRATEGY_REPORT.md` | 策略终版结论报告 |

> 迁移说明：以上 4 个文件原位于仓库根目录，2026-11-04 统一移入 `docs/analysis/`。
> `BACKTEST_LOG.md` / `WORKLOG.md` 中的历史条目仍按旧名引用它们（历史只追加不改写），
> 两者之间的相对链接（如 `ALPHA_ENGINE_ROADMAP.md` → `./ALPHA_CONCLUSIONS.md`）在同目录下仍有效。

## 五、推荐阅读顺序

1. `docs/overview.md` —— 先建立全局地图
2. `docs/flow.md` —— 再走一遍买卖主线
3. `docs/sas_formulas.md` —— 需要公式细节时查
4. `WORKLOG.md` / `BACKTEST_LOG.md` —— 想知道"为什么这么改/这个实验结论"时按条查

## 六、维护约定

- 改选股 / 评分 / 交易规则 / 回测参数：同步 `README.md` + `docs/sas_formulas.md` + `WORKLOG.md`。
- 回测 / 参数实验：写入 `BACKTEST_LOG.md`，编号唯一、只追加。
- 新增文档：一律放 `docs/`（研究类放 `docs/analysis/`），并更新本索引。
