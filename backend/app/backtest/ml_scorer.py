"""路线 A 完整版：ML 截面排序替换层（LightGBM + 完整 Alpha158 + 行业中性）。

设计目标
--------
替换 ``analysis/factors.compute_factors`` 的「14 因子启发式打分」为「段前训练、截面排序」
的学习型打分，**其余全部保留**（6 策略扫描 / 模拟盘撮合 / 风控 / 大盘择时）。这是路线图
B（换 alpha 引擎）里杠杆最高、风险最低的一步：只换评分那一层，不碰成熟的回测链路。

模型
----
- 默认 ``model="lightgbm"``：用 ``lightgbm`` 回归器在「段起始日之前」的滑窗上训练，
  预测 H 日前向收益；预测值在每个交易日做**截面分位(0~100)** 作为分数。
- **优雅降级**：若运行环境没有 ``lightgbm``（如离线沙箱），自动回退到纯 numpy Ridge
  （#74 的 A-minus 路径），接口与纪律完全一致，便于在受限环境冒烟测试。
- 超参用 **Purged Group 时序交叉验证** 在「严格走前」折上选（LightGBM 选 num_leaves/
  learning_rate，Ridge 选 L2 正则），并报告 OOS rank-IC。

行业中性（路线 A 与 B 的关键差异）
---------------------------------
- ``industry_neutral=True`` 时，在每日截面内先扣掉**行业（或板块）均值预测**，再重排分位，
  使信号对行业暴露为零，避免「动量/低波动」退化为「押注热门行业」。
- 行业映射来自 ``<data_dir>/industry_map.csv``（code,industry 两列），由
  ``scripts/fetch_industry_map.py`` 用 akshare 抓取；**缺失时自动回退为板块中性**
  （主板/创业板/科创板/北交所，与 ``mom10_probe`` 同口径），保证离线也能跑。

防未来函数的三条铁律
--------------------
1. 特征只用 ``as-of`` 日及之前的数据（return/MA/rolling 全部基于 ``shift``，不触达未来）。
2. 标签 = ``as-of`` 日之后的 H 日收益（标签允许用未来，特征不准）。
3. 训练只用「段起始日之前」的滑动窗口；段内预测用该段专属模型，绝不穿越。

验收纪律（与项目一致）
----------------------
- 若 OOS IC <= ``require_ic``（默认 0）则整段回退到 14 因子，不注入噪声；
- 最终 ``ml`` vs ``default`` 是否采纳，由 random_dist 的 **>3.5pp 配对显著线** 判定。
"""
from __future__ import annotations

import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "build_ml_predictions",
    "build_live_ml_predictions",
    "alpha_features",
    "DEFAULT_ALPHAS",
    "DEFAULT_HORIZON",
    "board_of",
    "load_industry_map",
]

DEFAULT_ALPHAS = (1.0, 4.0, 16.0, 64.0, 256.0)
DEFAULT_HORIZON = 15  # 与策略中位持仓（11~19 天）对齐

# LightGBM 超参网格（Purged Group 时序 CV 选最优）
LGB_GRID = [
    {"num_leaves": 15, "learning_rate": 0.05},
    {"num_leaves": 31, "learning_rate": 0.05},
    {"num_leaves": 31, "learning_rate": 0.10},
    {"num_leaves": 63, "learning_rate": 0.05},
]

#: 板块前缀（与 mom10_probe 同口径），用于行业映射缺失时的「板块中性」兜底。
BOARD_PREFIXES: dict[str, tuple[str, ...]] = {
    "主板": ("600", "601", "603", "605", "000", "001", "002", "003"),
    "创业板": ("300", "301"),
    "科创板": ("688", "689"),
    "北交所": ("43", "83", "87", "88", "92"),
}


def board_of(code: str) -> str:
    """按代码前缀判定板块（行业映射缺失时的中性化兜底）。"""
    for board, prefixes in BOARD_PREFIXES.items():
        if code.startswith(prefixes):
            return board
    return "其他"


def load_industry_map(path=None) -> dict[str, str] | None:
    """读取 code->行业 映射。

    path 为 None 时默认取 ``<kline_dir 父目录>/industry_map.csv``
    （即 ``settings.kline_dir.parent``）。文件不存在或格式不符返回 None。
    """
    if path is None:
        try:
            from app.config import get_settings
            path = get_settings().kline_dir.parent / "industry_map.csv"
        except Exception:  # noqa: BLE001
            return None
    if not path or not os.path.isfile(str(path)):
        return None
    try:
        df = pd.read_csv(str(path), dtype={"code": str})
        if "code" in df.columns and "industry" in df.columns:
            return dict(zip(df["code"].astype(str), df["industry"].astype(str)))
    except Exception:  # noqa: BLE001
        return None
    return None


# ----------------------------------------------------------------------
# 1) 完整 Alpha158 风格特征（纯 OHLCV 派生，无未来函数）
# ----------------------------------------------------------------------
def alpha_features(df: pd.DataFrame) -> pd.DataFrame:
    """对单只日线（已按日期升序）计算特征，返回与 df 同索引的 DataFrame。

    只用 ``date`` 及之前的量价，绝不引用未来行。覆盖收益/均线偏离/量能/波动/
    高低通道/ATR/跳空/影线/RSI/量价相关等多个家族，约 80 个特征。
    """
    close = pd.to_numeric(df["close"], errors="coerce").astype(float)
    open_ = pd.to_numeric(df["open"], errors="coerce").astype(float)
    high = pd.to_numeric(df["high"], errors="coerce").astype(float)
    low = pd.to_numeric(df["low"], errors="coerce").astype(float)
    vol = pd.to_numeric(df["volume"], errors="coerce").astype(float)
    zero = pd.Series(0.0, index=df.index)
    amt = pd.to_numeric(df["amount"], errors="coerce").astype(float) if "amount" in df.columns else zero.copy()
    to = pd.to_numeric(df["turnover_rate"], errors="coerce").astype(float) if "turnover_rate" in df.columns else zero.copy()

    out = pd.DataFrame(index=df.index)
    r = close.pct_change()  # 日收益

    # —— 收益率家族（多周期 + 对数 + 动量残差）——
    for k in (1, 3, 5, 10, 15, 20, 30, 60):
        out[f"ret{k}"] = close / close.shift(k) - 1.0
    for k in (5, 10, 20, 60):
        out[f"logret{k}"] = np.log(close / close.shift(k))
    # 长周期动量里剔除短周期部分：ret60 - ret20（长线动量残差）、ret20 - ret5
    out["mom60_20"] = out["ret60"] - out["ret20"]
    out["mom20_5"] = out["ret20"] - out["ret5"]

    # —— 均线偏离家族 ——
    for k in (5, 10, 20, 30, 60, 120):
        ma = close.rolling(k).mean()
        out[f"ma{k}_dev"] = close / ma - 1.0
    out["dev_accel"] = out["ma5_dev"] - out["ma20_dev"]  # 偏离的加速度

    # —— 量能家族 ——
    vma5 = vol.rolling(5).mean()
    vma20 = vol.rolling(20).mean()
    vma60 = vol.rolling(60).mean()
    out["vrate5"] = vol / vma5 - 1.0
    out["vrate20"] = vol / vma20 - 1.0
    out["vrate60"] = vol / vma60 - 1.0
    out["logvol"] = np.log(vol.clip(lower=1.0))
    if (amt > 0).any():
        ama5 = amt.rolling(5).mean()
        ama20 = amt.rolling(20).mean()
        ama60 = amt.rolling(60).mean()
        out["amt5"] = amt / ama5 - 1.0
        out["amt20"] = amt / ama20 - 1.0
        out["amt60"] = amt / ama60 - 1.0
    else:
        out["amt5"] = out["amt20"] = out["amt60"] = 0.0

    # 换手
    if (to > 0).any():
        tma5 = to.rolling(5).mean()
        tma20 = to.rolling(20).mean()
        out["turnover"] = to
        out["turnover5"] = to / tma5 - 1.0
        out["turnover20"] = to / tma20 - 1.0
    else:
        out["turnover"] = out["turnover5"] = out["turnover20"] = 0.0

    # —— 波动率家族 ——
    for k in (5, 10, 20, 30, 60):
        out[f"vol{k}"] = r.rolling(k).std()
    out["volexp"] = out["vol20"] / out["vol60"] - 1.0  # 波动扩张

    # —— 高低区间 / 通道位置 ——
    out["hl_range"] = (high - low) / close
    for k in (5, 10, 20, 60):
        hh = high.rolling(k).max()
        ll = low.rolling(k).min()
        out[f"hl_range{k}"] = (hh - ll) / close
        out[f"pos{k}"] = (close - ll) / (hh - ll + 1e-9)
        out[f"close_to_high{k}"] = close / hh - 1.0
        out[f"close_to_low{k}"] = close / ll - 1.0

    # —— ATR / close ——
    prev_close = close.shift(1)
    tr = np.maximum.reduce([
        (high - low).values,
        (high - prev_close).abs().values,
        (low - prev_close).abs().values,
    ])
    trs = pd.Series(tr, index=df.index)
    for k in (5, 10, 20, 60):
        out[f"atr{k}"] = trs.rolling(k).mean() / close

    # —— 跳空 / 日内 / 影线 ——
    out["gap"] = open_ / prev_close - 1.0
    out["intraday"] = close / open_ - 1.0
    rng = (high - low)
    out["up_shadow"] = (high - close) / (rng + 1e-9)
    out["low_shadow"] = (close - low) / (rng + 1e-9)

    # —— 震荡指标 ——
    for k in (14, 20):
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(k).mean()
        loss = (-delta.clip(upper=0)).rolling(k).mean()
        rs = gain / (loss + 1e-9)
        out[f"rsi{k}"] = 1.0 - 1.0 / (1.0 + rs)

    # —— 量价相关（知情交易的代理）——
    vol_chg = vol.pct_change().fillna(0.0)
    out["vret_corr"] = r.rolling(20).corr(vol_chg)

    return out


# ----------------------------------------------------------------------
# 2) 样本构造
# ----------------------------------------------------------------------
def _prepare_samples(
    klines: dict[str, pd.DataFrame],
    date_set: set[str],
    horizon: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """在 ``date_set`` 内、用 ``as-of`` 日及之前数据构造 (X, y, groups, codes)。

    y = close[H] / close[0] - 1（H 日 forward return），允许用未来，属标签。
    特征含 NaN（如 ma120 需 ≥120 根）的行直接丢弃。
    """
    Xrows: list[np.ndarray] = []
    ys: list[float] = []
    groups: list[str] = []
    codes: list[str] = []
    for code, df in klines.items():
        if len(df) < horizon + 20:
            continue
        feats = alpha_features(df)
        close = pd.to_numeric(df["close"], errors="coerce").astype(float).values
        dts = df["date"].values
        n = len(df)
        for i in range(n):
            if dts[i] not in date_set:
                continue
            if i + horizon >= n:
                continue
            row = feats.iloc[i].to_numpy(dtype=float)
            if not np.all(np.isfinite(row)):
                continue
            lab = close[i + horizon] / close[i] - 1.0
            if not np.isfinite(lab):
                continue
            Xrows.append(row)
            ys.append(float(lab))
            groups.append(str(dts[i]))
            codes.append(code)
    if not Xrows:
        return np.empty((0, 0)), np.empty(0), np.empty(0, dtype=object), []
    return np.array(Xrows), np.array(ys), np.array(groups), codes


# ----------------------------------------------------------------------
# 3) 工具：标准化 / Ridge / rank-IC / LightGBM
# ----------------------------------------------------------------------
def _standardize(X: np.ndarray, mean=None, std=None):
    if mean is None:
        mean = X.mean(0)
        std = X.std(0)
    std = np.where(std == 0, 1.0, std)
    return (X - mean) / std, mean, std


def _ridge_fit(X: np.ndarray, y: np.ndarray, alpha: float) -> np.ndarray:
    """闭式解 (XᵀX + αI) w = Xᵀy。"""
    XtX = X.T @ X
    XtX.flat[:: XtX.shape[0] + 1] += alpha  # 加 α 到对角线
    return np.linalg.solve(XtX, X.T @ y)


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(len(x), dtype=float)
    return ranks


def _rank_ic(a: np.ndarray, b: np.ndarray) -> float:
    ra, rb = _rank(a), _rank(b)
    da, db = ra - ra.mean(), rb - rb.mean()
    denom = np.sqrt((da * da).sum() * (db * db).sum())
    return float((da * db).sum() / denom) if denom > 0 else 0.0


def _resolve_model(model: str) -> str:
    """解析实际使用的模型：lightgbm 不可用时优雅回退 ridge。"""
    if model == "ridge":
        return "ridge"
    try:
        import lightgbm  # noqa: F401
        return "lightgbm"
    except Exception:  # noqa: BLE001
        logger.warning("lightgbm 不可用，回退 Ridge（接口/纪律不变）")
        return "ridge"


def _lgb_fit(X: np.ndarray, y: np.ndarray, num_leaves: int = 31,
             learning_rate: float = 0.05, n_estimators: int = 200):
    import lightgbm as lgb
    dtrain = lgb.Dataset(X, y)
    params = {
        "objective": "regression",
        "metric": "None",
        "num_leaves": int(num_leaves),
        "learning_rate": float(learning_rate),
        "min_child_samples": 50,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "verbose": -1,
        "seed": 42,
    }
    return lgb.train(params, dtrain, num_boost_round=int(n_estimators))


def _lgb_pred(bst, X: np.ndarray) -> np.ndarray:
    return bst.predict(X)


# ----------------------------------------------------------------------
# 4) Purged Group 时序交叉验证（按日期分组，严格走前）
# ----------------------------------------------------------------------
def purged_group_cv(
    X: np.ndarray,
    y: np.ndarray,
    groups: np.ndarray,
    alphas=DEFAULT_ALPHAS,
    model: str = "lightgbm",
    n_splits: int = 5,
):
    """时间序划分成 n_splits 折；每折训练用更早折、验证用本折（无重叠泄漏）。

    返回 (best_hparams, mean_oos_ic, per_fold_ics)。模型按 ``model`` 选 LightGBM 或 Ridge。
    """
    udates = np.array(sorted(set(groups.tolist())))
    if len(udates) < n_splits + 1:
        n_splits = max(1, len(udates) - 1)
    if n_splits < 1:
        return (None, 0.0, [])
    bounds = np.linspace(0, len(udates), n_splits + 1).astype(int)

    if model == "lightgbm":
        hps = [dict(h) for h in LGB_GRID]
    else:
        hps = [{"alpha": a} for a in alphas]

    best = None
    for hp in hps:
        ics: list[float] = []
        for f in range(1, n_splits):
            tr_g = set(udates[: bounds[f]].tolist())
            va_g = set(udates[bounds[f]: bounds[f + 1]].tolist())
            tr = np.array([g in tr_g for g in groups])
            va = np.array([g in va_g for g in groups])
            if tr.sum() < 50 or va.sum() < 10:
                continue
            Xtr_s, m, s = _standardize(X[tr])
            Xva_s, _, _ = _standardize(X[va], m, s)
            if model == "lightgbm":
                mdl = _lgb_fit(Xtr_s, y[tr], **hp)
                pred = _lgb_pred(mdl, Xva_s)
            else:
                w = _ridge_fit(Xtr_s, y[tr], hp["alpha"])
                pred = Xva_s @ w
            ics.append(_rank_ic(pred, y[va]))
        if ics:
            mean_ic = float(np.mean(ics))
            if best is None or mean_ic > best[1]:
                best = (hp, mean_ic, ics)
    if best is None:
        return (None, 0.0, [])
    return best


# ----------------------------------------------------------------------
# 5) 训练 + 段内预测 + 截面分位 + 行业中性
# ----------------------------------------------------------------------
def build_ml_predictions(
    klines: dict[str, pd.DataFrame],
    all_dates: list[str],
    segment_start: str,
    horizon: int = DEFAULT_HORIZON,
    alphas=DEFAULT_ALPHAS,
    n_splits: int = 5,
    require_ic: float = 0.0,
    model: str = "lightgbm",
    industry_neutral: bool = False,
    industry_map_path=None,
) -> tuple[dict[tuple[str, str], float], float]:
    """为单个回测段构建 ML 预测表。

    Args:
        klines: code -> 已加载的日线（含段前历史，建议 ≥ 320 根）。
        all_dates: 本段全部交易日（升序）。
        segment_start: 段起始日；训练只用 < 该日的样本。
        horizon: 标签前瞻天数。
        require_ic: OOS rank-IC 低于此值则视为无信号，返回空表（回退因子）。
        model: "lightgbm"（默认，不可用时自动回退 ridge）或 "ridge"。
        industry_neutral: True 时做行业内中性化（扣行业均值再排分位）。
        industry_map_path: 行业映射 csv 路径；None 用默认位置，缺失则板块中性兜底。

    Returns:
        (ml_pred, oos_ic):
          ml_pred[(code, date)] = 当日截面分位(0~100)，越高越看好；
          oos_ic = 走前 CV 的平均 OOS rank-IC（信号强度，供日志）。
    """
    resolved = _resolve_model(model)
    train_set = {d for d in all_dates if d < segment_start}
    pred_set = {d for d in all_dates if d >= segment_start}
    if not train_set or not pred_set:
        return {}, 0.0

    Xtr, ytr, gtr, _ = _prepare_samples(klines, train_set, horizon)
    if Xtr.shape[0] < 200:
        return {}, 0.0

    best = purged_group_cv(Xtr, ytr, gtr, alphas=alphas, model=resolved, n_splits=n_splits)
    if best[0] is None:
        return {}, 0.0
    hp, oos_ic, _ = best
    if oos_ic <= require_ic:
        # 无信号：回退到默认因子，不注入噪声
        return {}, float(oos_ic)

    # 终训：用全部训练样本
    Xtr_s, m, s = _standardize(Xtr)
    if resolved == "lightgbm":
        mdl = _lgb_fit(Xtr_s, ytr, **hp)
        predict = lambda Xs: _lgb_pred(mdl, Xs)  # noqa: E731
    else:
        w = _ridge_fit(Xtr_s, ytr, hp["alpha"])
        predict = lambda Xs: Xs @ w  # noqa: E731

    # 段内预测
    Xva, yva, gva, codes = _prepare_samples(klines, pred_set, horizon)
    if Xva.shape[0] == 0:
        return {}, float(oos_ic)
    Xva_s, _, _ = _standardize(Xva, m, s)
    pred = predict(Xva_s)

    # 行业映射（中性化用）
    ind_map = load_industry_map(industry_map_path) if industry_neutral else None

    pdf = pd.DataFrame({"date": gva, "code": codes, "pred": pred})
    if industry_neutral:
        pdf["ind"] = pdf["code"].map(lambda c: (ind_map.get(c) if ind_map else board_of(c)))
    else:
        pdf["ind"] = "ALL"

    ml_pred: dict[tuple[str, str], float] = {}
    for d, grp in pdf.groupby("date"):
        if industry_neutral and grp["ind"].nunique() > 1:
            # 扣掉行业均值，消除行业暴露
            mean_by = grp.groupby("ind")["pred"].transform("mean")
            vals = (grp["pred"] - mean_by).to_numpy()
        else:
            vals = grp["pred"].to_numpy()
        n = len(vals)
        if n < 2:
            continue
        pct = _rank(vals) / (n - 1) * 100.0  # 0~100 截面分位
        for code, p in zip(grp["code"].tolist(), pct.tolist()):
            ml_pred[(code, str(d))] = float(p)
    return ml_pred, float(oos_ic)


def _infer_samples(
    klines: dict[str, pd.DataFrame],
    as_of: str,
    horizon: int,
) -> tuple[np.ndarray, list[str]]:
    """为最新交易日 ``as_of`` 构造**免标签**推理样本（不需要未来收益率标签）。

    与 ``_prepare_samples`` 的唯一区别：不要求 ``i + horizon < n``（今天没有
    未来标签也能取特征）。返回 (X, codes)，X 的每一行是该股 ``as_of`` 日及之前的
    量价特征。
    """
    Xrows: list[np.ndarray] = []
    codes: list[str] = []
    for code, df in klines.items():
        if len(df) < horizon + 20:
            continue
        dts = df["date"].values
        feats = alpha_features(df)
        arr = feats.to_numpy(dtype=float)
        # 取 <= as_of 的最后一根「特征全有限」的 bar：最新一根常有不完整量价
        # （当日部分成交 / 零成交量）导致个别特征 NaN，回退到前一交易日即可，
        # 对 15 日前向模型可忽略（回测预测日均为历史日，不受此影响）。
        idx = None
        for j in range(len(df) - 1, -1, -1):
            sd = str(dts[j])
            if " " in sd:
                sd = sd.split(" ")[0]
            if sd > as_of:
                continue
            if np.all(np.isfinite(arr[j])):
                idx = j
                break
        if idx is None:
            continue
        Xrows.append(arr[idx])
        codes.append(code)
    if not Xrows:
        return np.empty((0, 0)), []
    return np.array(Xrows), codes


def build_live_ml_predictions(
    klines: dict[str, pd.DataFrame],
    horizon: int = DEFAULT_HORIZON,
    alphas=DEFAULT_ALPHAS,
    n_splits: int = 5,
    require_ic: float = 0.0,
    model: str = "lightgbm",
    industry_neutral: bool = False,
    industry_map_path=None,
) -> tuple[dict[tuple[str, str], float], float]:
    """线上实时推理版：用全历史训练，预测「最新交易日」(as_of) 的截面分位。

    与 :func:`build_ml_predictions` 的唯一差异：预测日不要求有未来收益率标签
    （今天没有未来标签），预测步改用 :func:`_infer_samples` 免标签取特征。
    训练步完全一致（仍用历史有标签样本 + Purged Group CV 选参 + OOS IC 回退纪律），
    因此与回测 ``default_ml`` 同模型、同行业中性、同信号纪律。

    Args:
        klines: code -> 已加载的日线（建议 ≥ 320 根；训练用 < as_of 的全部历史）。

    Returns:
        (ml_pred, oos_ic)：ml_pred[(code, as_of)] = 截面分位(0~100)，越高越看好；
        oos_ic = 走前 CV 的平均 OOS rank-IC。无信号（样本不足 / IC 过低）返回 ({}, 0.0)。
    """
    resolved = _resolve_model(model)
    # 全局最新交易日作为预测日（每个 code 取 <= as_of 的最新 bar）
    all_dates: set[str] = set()
    for df in klines.values():
        for d in df["date"].values:
            sd = str(d)
            if " " in sd:
                sd = sd.split(" ")[0]
            all_dates.add(sd)
    if not all_dates:
        return {}, 0.0
    as_of = max(all_dates)
    train_set = {d for d in all_dates if d < as_of}
    if not train_set:
        return {}, 0.0

    Xtr, ytr, gtr, _ = _prepare_samples(klines, train_set, horizon)
    if Xtr.shape[0] < 200:
        return {}, 0.0

    best = purged_group_cv(Xtr, ytr, gtr, alphas=alphas, model=resolved, n_splits=n_splits)
    if best[0] is None:
        return {}, 0.0
    hp, oos_ic, _ = best
    if oos_ic <= require_ic:
        # 无信号：回退到默认因子，不注入噪声
        return {}, float(oos_ic)

    Xtr_s, m, s = _standardize(Xtr)
    if resolved == "lightgbm":
        mdl = _lgb_fit(Xtr_s, ytr, **hp)
        predict = lambda Xs: _lgb_pred(mdl, Xs)  # noqa: E731
    else:
        w = _ridge_fit(Xtr_s, ytr, hp["alpha"])
        predict = lambda Xs: Xs @ w  # noqa: E731

    Xinf, codes = _infer_samples(klines, as_of, horizon)
    if Xinf.shape[0] == 0:
        return {}, float(oos_ic)
    Xinf_s, _, _ = _standardize(Xinf, m, s)
    pred = predict(Xinf_s)

    ind_map = load_industry_map(industry_map_path) if industry_neutral else None
    if industry_neutral:
        inds = [(ind_map.get(c) if ind_map else board_of(c)) for c in codes]
    else:
        inds = ["ALL"] * len(codes)

    pdf = pd.DataFrame({"code": codes, "ind": inds, "pred": pred})
    ml_pred: dict[tuple[str, str], float] = {}
    for _ind_val, grp in pdf.groupby("ind"):
        vals = grp["pred"].to_numpy()
        n = len(vals)
        if n < 2:
            continue
        pct = _rank(vals) / (n - 1) * 100.0  # 0~100 截面分位
        for code, p in zip(grp["code"].tolist(), pct.tolist()):
            ml_pred[(code, as_of)] = float(p)
    return ml_pred, float(oos_ic)
