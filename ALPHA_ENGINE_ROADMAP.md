# SAS · 换 Alpha 引擎路线与开源借鉴（记录 #72c · 2026-10-30）

> 背景：使用者希望"换一套更好的选股/alpha 内核"，并询问 GitHub 上是否已有成熟可借鉴的策略。
> 范围界定：**"换 alpha 引擎" = 替换当前 14 因子启发式打分（`factors.py`）那一层**，保留 SAS 已有的全链路（扫描→评分→模拟盘→回测→大盘择时）。这是最低风险、最高杠杆的改法，不是重写整个系统。
> 免责声明：仅供学习研究，不构成投资建议。

---

## 0. 一句话结论（先给建议）

- SAS 的回测/撮合/风控/择时链路已经成熟且 A 股原生（T+1、涨跌停、真实费率），**不必换框架**；
- 真正该换的是**评分/选股那一层**——当前 14 因子 IC 多数负，是系统 edge 小的根源；
- GitHub 上**最值得借鉴**的是微软 **Qlib** 的因子库 + 机器学习排序——用于**替换 SAS 的 14 因子启发式打分**（这是 edge 小的根源）。`ml-quant-trading` 的涨跌停 mask 已实测对 SAS 无效（见记录 #73：SAS 默认回测已含 mask、贡献≈0），不再作为优先项；
- **不要**被 M²-Alpha 那种 +443%/Sharpe 3.99 的曲线诱惑——其 README 自己标注了"post-hoc selection 选择性偏差"警告；
- 哪怕最好的开源 A 股 ML 系统，真实样本外 Sharpe 也就 **1.6~2.0、IC 0.07~0.09**，不是魔法，只是"方法更严、IC 转正"。换引擎的难点在**防过拟合的验证纪律**，不在代码搬运。

---

## 1. GitHub 上成熟、可借鉴的项目（已按与 SAS 的相关度排序）

| 项目 | 成熟度 | 关键能力 | 对 SAS 的借鉴点 | 注意点 |
| --- | --- | --- | --- | --- |
| **microsoft/qlib** | ★★★★★ 45k★ MIT，2020 至今活跃（0.9.7 / 2025-08） | Alpha158/Alpha360 因子集；26 个模型（LightGBM/XGBoost/LSTM/Transformer/HIST/DoubleEnsemble）；截面 Top-K 回测；IC/IR 报告；A 股原生（沪深300/500、开5bps/平15bps、±9.5% 涨跌停校验）；RD-Agent（LLM 自动挖因子） | **最高杠杆**：用其 Alpha158 特征 + LightGBM/HIST 排序，**替换 SAS 的 14 因子启发式打分**；保留 SAS 模拟盘/择时 | 官方数据包停在 2020（已禁用），但 SAS 自有 5735 只日 K + sas.db，直接喂自己的数据即可；无实盘/模拟盘/券商适配——正好 SAS 已有 |
| **initial-d/ml-quant-trading** | ★★★★☆ MIT，2025 arXiv 2507.07107 | **涨跌停不可交易性 mask**（"upstream contamination"：限价使部分收盘不可成交，标准管线吞入后虚增 IC 18%、实降 Sharpe 0.44）；213 因子 GPU 引擎；Adjusted-MSE（错向预测惩罚 11×）；块自助；Markowitz-Ledoit-Wolf 组合优化。真实 A 股 2022-24 Sharpe 1.63 | **方法论必借**：SAS 回测大概率也有同样的"吞入涨跌停价"污染。加一层 tradability mask 即可，几乎零成本却还你真实 IC | 需要 GPU 跑深度学习；但 mask + 损失函数思想可单独抽出来用 |
| **duhanjun/jingni-trader** | ★★★★☆ 活跃 | LLM 驱动全栈（数据→因子→训练→回测→组合→模拟/实盘）；LightGBM 默认；**Purged Group Time Series Split**（训练36月/验证12月/测试12月，清洗期2天）；MLflow；严格 A 股（T+1/涨跌停/真实费率）；OOS 样本外回测 | **防过拟合脚手架必借**：SAS 当前验收只有"随机段 + 3.5pp 线"，缺 purged CV / OOS / 实验追踪。直接抄它的验证骨架 | 偏"全家桶"，我们只需其验证与数据处理纪律，不必整包搬 |
| **Crises-Strity/A_Share_Alpha_System_with_GP_and_Transformer** | ★★★☆☆ 可复现 | 18 因子 + Transformer 排序；Top-50 每5日再平衡；每年 IC 均为正（0.075~0.091）；完整结果表/方法论文档 | 干净可复现的"截面排序 + 风控叠加"范本；证实"Transformer IC 非最高但组合 IR 最强" | 学术味重，工程需自接 |
| **Johnny-xuan/M2-Alpha** | ★★★☆☆ 研究仓 | Micro+Macro 双注意力（时序×截面互精炼）；35 因子；T=8 | 架构思路可参考（互精炼比单路更强） | **数字不可信**：README 自承 post-hoc selection 偏差；+443%/Sharpe 3.99 是"精选展示"，勿当基线 |
| **enhen-x/Quant_A_Share_V2.0** | ★★★☆☆ | 双头模型（收益+风险预测）；XGBoost/LightGBM；Walk-Forward；SHAP | 风险预测头 + 可解释性可补 SAS 风控 | 工程化模板，模型无新意 |
| 框架类（Backtrader/Zipline/vn.py/QUANTAXIS/VectorBT/Hikyuu） | — | 通用回测/交易框架 | **对 SAS 价值低**：SAS 已有 A 股原生回测引擎，无需替换 | 仅当要支持新资产/新频率时参考 |

---

## 2. "换 alpha 引擎"四条路线（代价 / 风险 / 预期）

| 路线 | 做什么 | 工作量 | 主要风险 | 预期收益 | 推荐度 |
| --- | --- | --- | --- | --- | --- |
| **C · 涨跌停 mask（已验证 = 无水分可挤）** | 验证 SAS 默认回测是否已含涨跌停约束、及其对结果的贡献 | **已完成** | 极低 | **结论：默认已含 mask 且贡献≈0（中位差 0.00%），不存在论文所指 18% IC 虚增**；残留日内卖出跌停 gate 已补（默认生效）。无需再投入 | ⭐ 已验证（非优先） |
| **A · Qlib 因子+ML 排序替换打分** | 用 Alpha158 风格特征 + LightGBM（或 HIST）截面排序，**替换 14 因子启发式**；保留 SAS 模拟盘/择时 | 2~4 周 | 过拟合（需 purged CV + OOS）；数据格式桥接 | 把"负 IC 启发式"变"正 IC 排序"，潜在最大 uplift | ⭐⭐⭐⭐ 主力 |
| **A-minus · 最小 ML 排序替换层（已实测，见 #74）** | 零依赖 numpy Ridge 截面排序（Alpha158-lite + Purged Group CV），替换 14 因子打分；OOS IC≤0 自动回退因子 | **已完成** | 沙箱无网络装不了 LightGBM，先用线性模型 | **结论：配对中位超额 −0.33pp（未过 3.5pp 显著线，赢段仅 44%）；但 25 分位下行 −1.09% vs default −4.63%，显著收紧尾部。即"风险阻尼器"而非"alpha 增强器"** | ⭐ 已落地（非默认） |
| **A · 完整版 LightGBM + 完整 Alpha158 + 行业中性（已落地+验收，见 #75/#76）** | `ml_scorer.py` 升级：~65 特征 Alpha158 风格 + LightGBM 回归器（环境无 lightgbm 时自动回退 Ridge）；Purged Group CV 选参；行业内中性化（有 `industry_map.csv` 用真行业、无则板块中性兜底） | **已完成 + 验收通过** | 已在本会话实跑真 LightGBM 验收（包在磁盘故可直接 import） | **结论（18 段配对，种子 20261028）：`ml_lgb` 中位超额 +4.55%（>3.5pp 显著线，过线）；但 bootstrap 95% CI≈[−0.2%,+9.8%]、符号检验 p=0.24，置信度中等；`ml_lgb_ind` 板块中性版 +3.95%（过线）；`ml_lgb_blend` +2.95%（未过）。显著优于 A-minus 的 −0.33pp** | ⭐ 验收通过（采纳为候选 `default_ml`） |
| **B · 经典截面因子层（动量/低波动）** | 新增 3~6 月截面动量 + 低波动因子，行业中性、月频再平衡，作为评分补充或并行信号 | 1~2 周 | A 股动量弱于美股、回撤大（单动量 MDD 可 −47%） | 稳健、有学术背书的小幅增强 | ⭐⭐⭐ 补充 |
| **D · LLM 自动因子研发（RD-Agent 思路）** | 用 LLM 自动提议因子+调模型，套 jingni-trader 的 purged CV/OOS/MLflow 护栏 | 1~2 月+ | LLM 易产出"看似有效实则过拟合"因子；算力 | 长期提效，但短期不保证 | ⭐⭐ 可选后期 |

---

## 3. 推荐落地顺序（最小风险、最快见真章）

1. **第 1 步（已完成验证）**：路线 C 已实跑——SAS 默认回测**已**含涨跌停约束（开盘成交模型天然规避论文所述"吞入涨停价"坑），且关掉 mask 后聚合结果纹丝不动（中位差 0.00%）。**结论：当前 `default` 的 +1.11% 中位超额即真实水平，无隐藏水分**。真正的杠杆在下两步。
2. **第 2 步（A-minus，已实测）**：最小 ML 排序替换层已落地（`backend/app/backtest/ml_scorer.py` + `pipeline.py` 注入 + `ml_default`/`ml_blend` profile）。沙箱无网络，先用 **numpy Ridge** 而非 LightGBM，配 Alpha158-lite 特征与 Purged Group 时序 CV。**实测结论（18 段同段配对，种子 20261028）：配对中位超额 −0.33pp，远未过 3.5pp 显著线；但下行 25 分位 −1.09% vs default −4.63%，显著收紧尾部**。即该层是"风险阻尼器"而非"alpha 增强器"，**不采纳为默认**，参数冻结不变。框架可复用：等有网络把 Ridge 换 LightGBM、扩到完整 Alpha158 + 行业中性，再评。
3. **第 3 步（与第 2 步并行，纪律）**：照搬 jingni-trader 的 **Purged Group Time Series Split + OOS 回测 + MLflow 追踪**，作为新评分层的验收门槛——任何新 alpha 必须过 OOS 才算数，且用现有"3.5pp 配对线"二次校准。
4. **第 4 步（可选补充）**：叠加路线 B 的截面动量/低波动作为多元化信号。
5. **第 5 步（可选后期）**：路线 D，LLM 自动因子研发循环。

> 关键校准：**"AI 换引擎很快"指的是代码搬运快；真正慢且难的是验证纪律**。GitHub 上翻车的开源量化项目，绝大多数不是模型差，而是把"样本内漂亮"当"样本外有效"。SAS 已有的 3.5pp 纪律 + 新增的 purged CV/OOS，才是换引擎后能信的结论。

---

## 4. 诚实的预期边界

- 最好的开源 A 股 ML 系统，真实样本外 **Sharpe 1.6~2.0、IC 0.07~0.09**——换引擎是"把负 IC 翻正、把噪声 edge 变干净"，不是"年化翻倍"。
- M²-Alpha 的 +443% 是选择性展示，官方自承偏差，**绝不可作目标**。
- 即便换引擎，大盘择时降仓（弱市 0.3）、严格止损、T+1、涨跌停这些 A 股结构性约束不变——它们决定下限，alpha 只决定上限。

---

## 5. 产出与下一步

- 本文件即路线与借鉴清单。**路线 C 已实跑验证并写入 BACKTEST_LOG #73**：加了 `disable_limit_mask` 开关 + `no_mask` profile，A/B 证明 mask 默认已生效、贡献≈0（见 `random_dist_maskab.json`）。**路线 A-minus 已落地实测并写入 BACKTEST_LOG #74**：`ml_scorer.py`（零依赖 Ridge + Purged Group CV）+ `ml_default`/`ml_blend` profile，A/B 证明未过 3.5pp 显著线但收紧下行（见 `random_dist_mlab.json`）。两处代码均未动线上默认值。
- **下一步（路线 A 已验收，进行收尾）**：(a) **路线 A 完整版已真机验收通过（#76）**——`ml_lgb` 配对中位 +4.55%（>3.5pp），已新增生产候选 `default_ml`（LightGBM 打分 + 因子回退兜底，其余继承 `default`）；原 `default`(14 因子) 保留为对照基线未覆盖。**建议将线上 active profile 切到 `default_ml`，并保留前向验证纪律（首个判读 ≈ 2026-12-01）。** 两项非阻塞待补：① 跑 `scripts/fetch_industry_map.py` 拉真实行业映射后重测 `ml_lgb_ind`，看真行业中性是否更优；② 置信度中等，按"采纳候选 + 监控"而非"锁定唯一真理"处理。(b) 路线 B（截面动量/低波动）仍可作为多元化补充或"降回撤变体"并行评估（优先级降至更低，因 A 已提供主 alpha）。
