# -*- coding: utf-8 -*-
"""事前预估摆动转折的**条件分布**（BACKTEST_LOG #121），只观测不交易。

为什么不是「预估极值点」：
- 「t 是极值」依赖 t 之后的路径，定义上就是事后概念，事前不可观测；
- 实测（#121 探针）显示提前 1/2/3 根买入的收益 6.65/5.48/4.61%，
  反而低于等确认的 4.98% —— 更早埋伏不是解法，值钱的是「极值当天零滞后识别」；
- 因此这里只输出 ``p_turn``（转折 hazard）+ 条件幅度分布，不输出任何点位或买卖日期。

三条因果红线（tests/test_swing_estimate.py 逐条锁死）：
1. 特征只用截至 ``bar`` 的数据：滚动 Z / 连续下跌 / 距最近**已可见**摆动点 /
   波动代理 / 基准 MA60 全是因果量；
2. 标签（未来是否转折、波段幅度）可以用未来数据，但样本带 ``available_bar``，
   只有标签窗口完全走完（配对高点的 ``confirmed_bar``）才允许入库；
3. 建表按 ``available_bar <= t`` 增量推进，逐日重放等价于「当时真的只看得到这些历史」。

个股历史怎么用：单票摆动低点中位数只有 11 个（p10=5 / p90=15），单独建模必然过拟合。
所以全局按特征组合分箱出先验，个股只做**带上限的 Beta 收缩**（等效样本数封顶），
即「根据个股历史」只能对全局先验做有限修正，不能自己说了算。
"""
from __future__ import annotations

import bisect
import heapq
import math
import statistics
from dataclasses import dataclass, field, replace

import numpy as np
import pandas as pd

from app.analysis.baseline import rolling_zscore
from app.analysis.swing import SwingPoint

_NAN = float("nan")

# 全部候选特征，顺序即分箱矩阵列顺序。``z`` 必须保留：它的 -1 值同时承担
# 「滚动窗口尚未走完，该 bar 不可用」的掩码职责（build_samples_and_queries 依赖）。
# 前 5 个是 #121 基线；后 3 个是 #122 引入的基准节奏 / 领先型特征。
FEATURE_NAMES: tuple[str, ...] = (
    "z", "streak", "since", "vol", "regime", "bench_mom", "lead", "bench_z",
)
BASE_FEATURES = "z,streak,since,vol,regime"

#: #123 概率校准方式：``none`` = 直接用分箱收缩出的原始概率（#121/#122 口径），
#: ``platt`` = 在 logit 空间做 2 参数 logistic 重标定（只改水平，不改排序）。
CALIBRATION_KINDS: tuple[str, ...] = ("none", "platt")


def parse_features(spec: str) -> tuple[str, ...]:
    """解析逗号分隔的特征名；强制 ``z`` 在首位，拒绝未知名与重复。"""
    names = tuple(x.strip() for x in spec.split(",") if x.strip())
    if not names or "z" not in names:
        raise ValueError("特征集必须包含 z（滚动窗口掩码）")
    unknown = [x for x in names if x not in FEATURE_NAMES]
    if unknown:
        raise ValueError(f"未知特征：{unknown}")
    if len(set(names)) != len(names):
        raise ValueError("特征名重复")
    return ("z",) + tuple(x for x in names if x != "z")


@dataclass(frozen=True)
class EstimateConfig:
    """预估窗口、收缩强度与发信号门槛；默认值与 ``Settings.swing_est_*`` 对齐。"""

    horizon: int = 3
    prior_strength: float = 40.0
    symbol_cap: int = 40
    entry_lift: float = 2.0
    cost_pct: float = 0.3
    min_exp_net: float = 0.0
    z_window: int = 60
    confirm_bars: int = 3
    min_global_samples: int = 30
    features: str = BASE_FEATURES
    # ---- #123 概率校准（Platt）：只修「概率水平」，不改变分箱特征与排序 ----
    calibration: str = "none"
    calib_min_samples: int = 200      # 少于这个样本数就不启用校准（直接用原始 p）
    calib_refit_every: int = 20       # 每多少根 bar 重拟合一次（只用当时已入库样本）
    calib_l2: float = 1.0             # 向 (a=1, b=0) 收缩的强度，防止少样本时爆炸

    def validate(self) -> None:
        if self.horizon < 1 or self.confirm_bars < 1:
            raise ValueError("horizon 与 confirm_bars 至少为 1")
        if self.prior_strength <= 0 or self.symbol_cap < 1:
            raise ValueError("先验强度必须为正，个股样本上限至少 1")
        if self.entry_lift <= 1.0:
            raise ValueError("entry_lift 需大于 1（必须显著高于基础率才发信号）")
        if self.z_window < 5 or self.min_global_samples < 1:
            raise ValueError("滚动窗口 / 最小样本数不合法")
        if self.calibration not in CALIBRATION_KINDS:
            raise ValueError(f"calibration 只支持 {CALIBRATION_KINDS}")
        if self.calib_min_samples < 10 or self.calib_refit_every < 1:
            raise ValueError("校准最小样本数至少 10，重拟合间隔至少 1 根")
        if self.calib_l2 <= 0:
            raise ValueError("校准 L2 强度必须为正")
        parse_features(self.features)


def estimate_config_from_settings() -> EstimateConfig:
    """参数唯一入口：``.env`` / app_state 覆盖层 / 代码默认值都在这里收口。"""
    from app.config import get_settings

    s = get_settings()
    cfg = EstimateConfig(
        horizon=int(s.swing_est_horizon),
        prior_strength=float(s.swing_est_prior_strength),
        symbol_cap=int(s.swing_est_symbol_cap),
        entry_lift=float(s.swing_est_entry_lift),
        cost_pct=float(s.swing_est_cost_pct),
        min_exp_net=float(s.swing_est_min_exp_net),
        z_window=int(s.swing_z_window),
        confirm_bars=int(s.swing_confirm_bars),
        features=str(s.swing_est_features),
        calibration=str(s.swing_est_calibration),
        calib_min_samples=int(s.swing_est_calib_min_samples),
        calib_refit_every=int(s.swing_est_calib_refit_every),
        calib_l2=float(s.swing_est_calib_l2),
    )
    cfg.validate()
    return cfg


@dataclass(frozen=True)
class SymbolScan:
    """一只票扫描后的最小输入；``first_bar`` 是 warmup + 起始日期之后的首根有效 bar。"""

    code: str
    rs: pd.Series
    points: list[SwingPoint]
    first_bar: int


@dataclass(frozen=True)
class TurnSample:
    """一条训练样本；标签可用未来，但 ``available_bar`` 之前绝不入库。"""

    code: str
    available_bar: int
    key: tuple[int, ...]
    y_turn: int
    downside_pct: float
    upside_pct: float


@dataclass(frozen=True)
class TurnEstimate:
    """条件分布，不是点位预测。``exp_net_pct`` 扣掉双边成本后的期望净收益。

    ``p_raw`` 是分箱 + 个股收缩的原始概率，``p_turn`` 是经过（可选的）在线
    Platt 校准后的概率。#123 把两者同时保留，是为了能回答「校准到底改了水平
    还是只改了排序」——只留一个数就无从对照。
    """

    p_turn: float
    exp_upside_pct: float
    exp_downside_pct: float
    exp_net_pct: float
    horizon: int
    global_samples: int
    symbol_samples: int
    symbol_weight: float
    p_raw: float = 0.0
    calibrated: bool = False

    def as_dict(self) -> dict:
        return {
            "p_turn": round(self.p_turn, 4),
            "p_raw": round(self.p_raw, 4),
            "calibrated": self.calibrated,
            "exp_upside_pct": round(self.exp_upside_pct, 3),
            "exp_downside_pct": round(self.exp_downside_pct, 3),
            "exp_net_pct": round(self.exp_net_pct, 3),
            "horizon": self.horizon,
            "global_samples": self.global_samples,
            "symbol_samples": self.symbol_samples,
            "symbol_weight": round(self.symbol_weight, 3),
        }


@dataclass(frozen=True)
class _Query:
    """一次「站在 bar 当时」的预估请求；``y_truth`` 只在事后评估校准度时用。

    ``available_bar`` 与同一条 ``TurnSample`` 完全一致：校准器只能在这条标签
    窗口走完之后吃到 (p_raw, y) 这一对，否则就变成了用未来标签修当前概率。
    """

    bar: int
    code: str
    key: tuple[int, ...]
    y_truth: int
    forward_pct: float
    confirmed_pct: float
    loss_pct: float
    available_bar: int = 0


@dataclass
class _Stat:
    n: int = 0
    n_pos: int = 0
    sum_up: float = 0.0
    sum_down: float = 0.0
    n_neg: int = 0

    def add(self, s: TurnSample) -> None:
        self.n += 1
        if s.y_turn:
            self.n_pos += 1
            self.sum_up += max(0.0, s.upside_pct)
        else:
            self.n_neg += 1
            self.sum_down += max(0.0, -s.downside_pct)

    def rates(self) -> tuple[float, float, float]:
        """Laplace 平滑的概率、转折时上涨均值、未转折时下探均值。"""
        p = (self.n_pos + 1.0) / (self.n + 2.0)
        up = self.sum_up / self.n_pos if self.n_pos else 0.0
        down = self.sum_down / self.n_neg if self.n_neg else 0.0
        return p, up, down


def build_feature_buckets(
    rs: pd.Series,
    points: list[SwingPoint],
    bench_close: pd.Series,
    cfg: EstimateConfig,
    features: str | None = None,
) -> np.ndarray:
    """返回 ``(n, len(names))`` 的分箱矩阵，每一行只用到该 bar 及之前的数据。

    #121 的 5 个基线特征：RS 偏离 Z / 连续下跌根数 / 距最近**已可见**摆动点 /
    波动代理 / 基准 MA60。Z 不足窗口时为 -1，调用方跳过该 bar。

    #122 新增 3 个「基准节奏 / 领先型」特征，全部是 trailing 窗口，因果性由
    tests/test_swing_estimate.py 逐列锁死：
    - ``bench_mom``：基准近 5 根收益（大盘自己走到哪一步）；
    - ``lead``：个股 RS 近 5 根变化减去基准近 5 根变化 —— 「基准没转向、
      个股先转向」的量化代理，正是 #121 结论里点名的下一批候选特征；
    - ``bench_z``：基准相对自身 60 日均线/标准差的位置（基准处于低位还是高位）。

    默认 ``features`` 取 ``cfg.features``；#121 的默认值仍是原 5 列，
    因此已有产物与单测的数值不会因为本次扩展而改变。
    """
    cfg.validate()
    names = parse_features(features if features is not None else cfg.features)
    arr = rs.to_numpy(dtype=float)
    n = len(arr)
    bench = pd.Series(bench_close).astype(float).to_numpy(dtype=float)

    z = rolling_zscore(rs, cfg.z_window).to_numpy(dtype=float)
    z_bucket = np.full(n, -1, dtype=int)
    z_bucket[(z < -1.0)] = 0
    z_bucket[(z >= -1.0) & (z < 0.0)] = 1
    z_bucket[(z >= 0.0) & (z < 1.0)] = 2
    z_bucket[(z >= 1.0)] = 3

    diff = np.diff(arr, prepend=arr[0])
    streak = np.zeros(n, dtype=float)
    for i in range(1, n):
        streak[i] = streak[i - 1] + 1.0 if diff[i] < 0 else 0.0
    streak_bucket = np.digitize(streak, [2.0, 4.0])

    # 距最近已确认摆动点：只按 confirmed_bar 推进指针，等价于 visible_swings(bar)
    confirmed = sorted((p.confirmed_bar, p.pivot_bar) for p in points)
    since = np.full(n, 999, dtype=int)
    ptr, last = 0, -(10**9)
    for i in range(n):
        while ptr < len(confirmed) and confirmed[ptr][0] <= i:
            last = confirmed[ptr][1]
            ptr += 1
        since[i] = i - last if last >= 0 else 999
    since_bucket = np.digitize(since, [5, 15])

    ret = np.abs(diff / np.maximum(np.abs(arr), 1e-9))
    vol = pd.Series(ret).rolling(20, min_periods=20).mean().to_numpy(dtype=float)
    vol_med = pd.Series(vol).rolling(60, min_periods=60).median().to_numpy(dtype=float)
    vol_bucket = np.where(
        np.isnan(vol) | np.isnan(vol_med), 0, (vol > vol_med).astype(int)
    )

    bs = pd.Series(bench)
    ma = bs.rolling(60, min_periods=60).mean().to_numpy(dtype=float)
    regime_bucket = np.where(np.isnan(ma), 0, (bench >= ma).astype(int))

    # np.roll 会把尾部绕回头部，前 5 根必须显式归零，否则就是前视泄漏
    m = 5
    bench_prev = np.roll(bench, m)
    rs_prev = np.roll(arr, m)
    with np.errstate(divide="ignore", invalid="ignore"):
        bench_mom = bench / np.maximum(np.abs(bench_prev), 1e-9) - 1.0
        rs_mom = arr / np.maximum(np.abs(rs_prev), 1e-9) - 1.0
    bench_mom[:m], rs_mom[:m] = 0.0, 0.0
    bench_mom = np.nan_to_num(bench_mom, nan=0.0, posinf=0.0, neginf=0.0)
    rs_mom = np.nan_to_num(rs_mom, nan=0.0, posinf=0.0, neginf=0.0)
    bench_mom_bucket = np.digitize(bench_mom, [-0.01, 0.01])
    lead_bucket = np.digitize(rs_mom - bench_mom, [-0.01, 0.01])

    sd = bs.rolling(60, min_periods=60).std(ddof=0).to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        bench_z = (bench - ma) / np.maximum(sd, 1e-9)
    bench_z_bucket = np.where(
        np.isnan(bench_z), 1, np.digitize(np.nan_to_num(bench_z, nan=0.0), [-1.0, 0.0, 1.0])
    )

    cols = {
        "z": z_bucket, "streak": streak_bucket, "since": since_bucket,
        "vol": vol_bucket, "regime": regime_bucket,
        "bench_mom": bench_mom_bucket, "lead": lead_bucket, "bench_z": bench_z_bucket,
    }
    return np.column_stack(tuple(cols[x] for x in names)).astype(int)


def build_samples_and_queries(
    scan: SymbolScan,
    buckets: np.ndarray,
    cfg: EstimateConfig,
) -> tuple[list[TurnSample], list[_Query]]:
    """同一份逐 bar 数据既产出训练样本，也产出「站在当时」的预估请求。"""
    cfg.validate()
    arr = scan.rs.to_numpy(dtype=float)
    n = len(arr)
    lows = sorted((p for p in scan.points if p.kind == "low"), key=lambda p: p.pivot_bar)
    highs = sorted((p for p in scan.points if p.kind == "high"), key=lambda p: p.pivot_bar)
    low_pivots = [p.pivot_bar for p in lows]
    high_pivots = [p.pivot_bar for p in highs]

    samples: list[TurnSample] = []
    queries: list[_Query] = []
    for s in range(max(0, scan.first_bar), n - cfg.horizon):
        if buckets[s][0] < 0 or arr[s] <= 0:
            continue
        key = tuple(int(x) for x in buckets[s])
        j = bisect.bisect_left(low_pivots, s)
        low = lows[j] if j < len(lows) else None
        turning = low is not None and low.pivot_bar <= s + cfg.horizon
        h = bisect.bisect_right(high_pivots, low.pivot_bar) if turning else 0
        high = highs[h] if turning and h < len(highs) else None

        if turning and high is not None:
            y = 1
            downside = (low.value / arr[s] - 1.0) * 100.0
            upside = (high.value / arr[s] - 1.0) * 100.0
            entry = arr[low.confirmed_bar] if low.confirmed_bar < n else arr[s]
            confirmed = (high.value / entry - 1.0) * 100.0 if entry > 0 else 0.0
            # 幅度用到了配对高点的 pivot 值，必须等该高点被确认才能入库
            available = high.confirmed_bar
            loss = 0.0
        elif turning:
            y = 1
            downside = (low.value / arr[s] - 1.0) * 100.0
            upside, confirmed, loss = 0.0, 0.0, 0.0
            available = low.confirmed_bar + cfg.horizon + 1
        else:
            y = 0
            window = arr[s:s + cfg.horizon + 1]
            downside = (float(window.min()) / arr[s] - 1.0) * 100.0 if window.size else 0.0
            upside, confirmed = 0.0, 0.0
            loss = abs(downside)
            # 没有低点这一结论，也要等确认窗口走完才成立
            available = s + cfg.horizon + cfg.confirm_bars + 1

        avail = max(available, s + 1)
        samples.append(TurnSample(
            code=scan.code,
            available_bar=avail,
            key=key,
            y_turn=y,
            downside_pct=downside,
            upside_pct=upside,
        ))
        queries.append(_Query(
            bar=s, code=scan.code, key=key, y_truth=y,
            forward_pct=upside, confirmed_pct=confirmed, loss_pct=loss,
            available_bar=avail,
        ))
    return samples, queries


_EPS = 1e-4


def _logit(p: float, eps: float = _EPS) -> float:
    p = min(max(float(p), eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


class PlattCalibrator:
    """在线（expanding-window）2 参数 Platt 校准：``p' = sigmoid(a·logit(p) + b)``。

    为什么需要：#122 实测概率是**系统性低估**（bias −0.0105、slope 1.58）——
    排序有信息，但绝对水平被压缩，直接拿概率定仓位会低估转折频率。

    为什么是 Platt 而不是等渗回归：单票摆动低点中位数只有 11 个，等渗在小样本
    下容易过拟合并无法外推；2 参数模型只回答「放大多少、整体偏移多少」，稳健，
    且 a > 0 时完全保留原排序，不会悄悄改变选股顺序。

    三条因果约束（tests/test_swing_calibration.py 逐条锁死）：
    1. 只吃 ``available_bar <= t`` 的 (p_raw, y) 对，标签窗口没走完绝不入表；
    2. 定期重拟合而非每根 bar 重拟合，避免用同一批样本反复调参；
    3. L2 向恒等映射 (a=1, b=0) 收缩，样本少时自动退化成「不校准」。
    """

    def __init__(self, cfg: EstimateConfig):
        self.cfg = cfg
        self.a = 1.0
        self.b = 0.0
        self.n = 0
        self.refits = 0
        self.fitted_at = -(10**9)
        # 聚合到「x 取两位小数」的桶：唯一 x 远少于样本数，重拟合才够快
        self._agg: dict[float, list[float]] = {}

    @property
    def ready(self) -> bool:
        return self.n >= self.cfg.calib_min_samples

    def add(self, x: float, y: int) -> None:
        key = round(float(x), 2)
        rec = self._agg.get(key)
        if rec is None:
            self._agg[key] = [1.0, float(y)]
        else:
            rec[0] += 1.0
            rec[1] += float(y)
        self.n += 1

    def maybe_refit(self, bar: int) -> None:
        if not self.ready:
            return
        if bar - self.fitted_at < self.cfg.calib_refit_every:
            return
        self.refit()
        self.fitted_at = bar

    def _objective(self, a: float, b: float, xs, w, ys, lam: float) -> float:
        z = a * xs + b
        s = np.clip(1.0 / (1.0 + np.exp(-z)), 1e-9, 1.0 - 1e-9)
        ll = float(np.dot(w, ys * np.log(s) + (1.0 - ys) * np.log(1.0 - s)))
        return -ll + 0.5 * lam * ((a - 1.0) ** 2 + b ** 2)

    def refit(self) -> None:
        """加权 Newton + 回溯步长；L2 保证 a/b 不会因少样本或完全分离而发散。"""
        if not self._agg:
            return
        xs = np.fromiter(self._agg.keys(), dtype=float, count=len(self._agg))
        w = np.array([v[0] for v in self._agg.values()], dtype=float)
        ys = np.array([v[1] for v in self._agg.values()], dtype=float) / w
        lam = float(self.cfg.calib_l2)
        a, b = 1.0, 0.0
        cur = self._objective(a, b, xs, w, ys, lam)
        for _ in range(25):
            z = a * xs + b
            s = 1.0 / (1.0 + np.exp(-z))
            h = w * s * (1.0 - s)
            g_a = float(np.dot((ys - s) * w, xs) - lam * (a - 1.0))
            g_b = float(np.sum((ys - s) * w) - lam * b)
            h_aa = float(np.dot(h, xs * xs)) + lam
            h_ab = float(np.dot(h, xs))
            h_bb = float(np.sum(h)) + lam
            det = h_aa * h_bb - h_ab * h_ab
            if det <= 1e-12:
                break
            d_a = (h_bb * g_a - h_ab * g_b) / det
            d_b = (h_aa * g_b - h_ab * g_a) / det
            step = 1.0
            for _ in range(12):
                na = float(np.clip(a + step * d_a, 0.02, 20.0))
                nb = float(np.clip(b + step * d_b, -8.0, 8.0))
                obj = self._objective(na, nb, xs, w, ys, lam)
                if obj <= cur + 1e-12:
                    break
                step *= 0.5
            else:
                break
            moved = abs(na - a) + abs(nb - b)
            a, b = na, nb
            cur = obj
            if moved < 1e-6:
                break
        self.a, self.b = a, b
        self.refits += 1

    def apply(self, p: float) -> float:
        if not self.ready:
            return p
        return _sigmoid(self.a * _logit(p) + self.b)

    def summary(self) -> dict:
        return {
            "kind": self.cfg.calibration,
            "ready": self.ready,
            "samples": self.n,
            "refits": self.refits,
            "a": round(self.a, 4),
            "b": round(self.b, 4),
            "l2": self.cfg.calib_l2,
            "min_samples": self.cfg.calib_min_samples,
            "refit_every": self.cfg.calib_refit_every,
        }


class TurnEstimator:
    """在线（walk-forward）条件分布表：按 ``available_bar`` 增量入库，不回头重训。"""

    def __init__(self, cfg: EstimateConfig, samples: list[TurnSample]):
        cfg.validate()
        self.cfg = cfg
        self.samples = sorted(samples, key=lambda x: x.available_bar)
        self.ptr = 0
        self.current_bar = -1
        self.total_n = 0
        self.total_pos = 0
        self.by_key: dict[tuple[int, ...], _Stat] = {}
        self.by_code: dict[str, _Stat] = {}
        self.calib = PlattCalibrator(cfg) if cfg.calibration != "none" else None
        # 校准样本用最小堆按 available_bar 排队：查询按 bar 递增产生，但标签
        # 可用时间不一定单调（配对高点可能更晚确认），堆能保证严格按可用时间入库。
        self._pending: list[tuple[int, float, int]] = []

    def advance_to(self, bar: int) -> None:
        bar = max(bar, self.current_bar)
        while self.ptr < len(self.samples) and self.samples[self.ptr].available_bar <= bar:
            s = self.samples[self.ptr]
            self.by_key.setdefault(s.key, _Stat()).add(s)
            self.by_code.setdefault(s.code, _Stat()).add(s)
            self.total_n += 1
            self.total_pos += s.y_turn
            self.ptr += 1
        if self.calib is not None:
            while self._pending and self._pending[0][0] <= bar:
                _, x, y = heapq.heappop(self._pending)
                self.calib.add(x, y)
            self.calib.maybe_refit(bar)
        self.current_bar = bar

    def observe(self, available_bar: int, p_raw: float, y: int) -> None:
        """登记一对「当时给出的概率 + 事后标签」；到 ``available_bar`` 才允许入库。"""
        if self.calib is None:
            return
        heapq.heappush(self._pending, (int(available_bar), _logit(p_raw), int(y)))

    def base_rate(self) -> float:
        """当时**已入库**样本的转折基础率（Laplace 平滑，冷启动时天然保守）。"""
        return (self.total_pos + 1.0) / (self.total_n + 2.0)

    def estimate(self, code: str, key: tuple[int, ...]) -> TurnEstimate | None:
        """全局分箱先验 + 个股 Beta 收缩 + 可选在线 Platt 校准。"""
        g = self.by_key.get(key)
        if g is None or g.n < self.cfg.min_global_samples:
            return None
        p_g, up_g, down_g = g.rates()
        c = self.by_code.get(code)
        k = c.n if c else 0
        eff = min(k, self.cfg.symbol_cap)
        w = eff / (eff + self.cfg.prior_strength)
        if c is not None and k > 0:
            p_c, up_c, down_c = c.rates()
            p_raw = w * p_c + (1.0 - w) * p_g
            up = w * up_c + (1.0 - w) * up_g
            down = w * down_c + (1.0 - w) * down_g
        else:
            p_raw, up, down = p_g, up_g, down_g
        applied = self.calib is not None and self.calib.ready
        p = self.calib.apply(p_raw) if applied else p_raw
        # 期望净收益用校准后的概率：低估会把 exp_net 系统性算高，正是 #122 的
        # 「模型预估 0.519% vs 实得 0.21%」那类偏差的来源之一。
        net = p * up - (1.0 - p) * down - self.cfg.cost_pct
        return TurnEstimate(
            p_turn=p, exp_upside_pct=up, exp_downside_pct=down,
            exp_net_pct=net, horizon=self.cfg.horizon,
            global_samples=g.n, symbol_samples=k, symbol_weight=w,
            p_raw=p_raw, calibrated=applied,
        )

    def calibrator_summary(self) -> dict:
        return self.calib.summary() if self.calib is not None else {"kind": "none"}


def _median(xs: list[float]) -> float | None:
    return round(statistics.median(xs), 3) if xs else None


@dataclass(frozen=True)
class EstimateDataset:
    """一次扫描产出的全部样本与查询；网格里同一份 dataset 可复用于多组参数。

    只有 ``features / horizon / z_window / confirm_bars`` 会影响它的内容，
    ``prior_strength / symbol_cap / entry_lift / min_exp_net`` 只影响重放统计，
    所以敏感性网格先按这四项缓存 dataset，避免几十组配置重复逐 bar 造样本。
    """

    samples: list[TurnSample]
    queries: list[_Query]
    features: str


def build_dataset(
    scans: list[SymbolScan],
    bench_close: pd.Series,
    cfg: EstimateConfig,
) -> EstimateDataset:
    cfg.validate()
    all_samples: list[TurnSample] = []
    all_queries: list[_Query] = []
    for scan in scans:
        buckets = build_feature_buckets(scan.rs, scan.points, bench_close, cfg)
        samples, queries = build_samples_and_queries(scan, buckets, cfg)
        all_samples.extend(samples)
        all_queries.extend(queries)
    # 网格会复用同一份 dataset 跑几十组参数；预排序让 TurnEstimator 里的
    # sorted() 退化成 O(n) 扫描（Timsort 最好情况），省掉重复的 10 万级排序。
    all_samples.sort(key=lambda s: s.available_bar)
    all_queries.sort(key=lambda q: q.bar)
    return EstimateDataset(all_samples, all_queries, cfg.features)


def _reliability(
    pairs: list[tuple[_Query, TurnEstimate]], attr: str = "p_turn"
) -> tuple[list[dict], dict]:
    """等频十分箱可靠性曲线 + ECE / 偏差 / 斜率。

    #121 用的是等宽 5 箱，但 ``p_turn`` 99% 落在 0–0.2，5 箱里 3 箱为空，
    曲线根本读不出来。改按预测概率排序等频切分，每箱样本量相同：
    - ``predicted > actual`` → 过度自信；``<`` → 低估（#121/#122 实测是低估）；
    - ``slope > 0`` → 概率排序仍带信息，哪怕绝对水平有偏；``slope > 1``
      还说明幅度被压缩（#123 用 Platt 校准直接修这一项）；
    - ``ece`` → 拿概率去定仓位前必须先看的整体校准误差。

    ``attr`` 用来对同一批重放结果分别算「校准前 (p_raw)」与「校准后 (p_turn)」，
    否则无法回答校准究竟改善了水平还是只改了排序。
    """
    total = len(pairs)
    if not total:
        return [], {"calibration_ece": None, "calibration_bias": None, "calibration_slope": None}
    ordered = sorted(pairs, key=lambda qe: (getattr(qe[1], attr), qe[0].bar))
    k = min(10, total)
    bins: list[dict] = []
    for i in range(k):
        lo, hi = i * total // k, (i + 1) * total // k
        group = ordered[lo:hi]
        if not group:
            continue
        pred = sum(getattr(e, attr) for _, e in group) / len(group)
        actual = sum(q.y_truth for q, _ in group) / len(group)
        bins.append({
            "bin": f"q{i + 1:02d}",
            "count": len(group),
            "p_low": round(min(getattr(e, attr) for _, e in group), 4),
            "p_high": round(max(getattr(e, attr) for _, e in group), 4),
            "predicted": round(pred, 4),
            "actual": round(actual, 4),
        })
    ece = sum(b["count"] * abs(b["predicted"] - b["actual"]) for b in bins) / total
    mean_pred = sum(getattr(e, attr) for _, e in pairs) / total
    mean_actual = sum(q.y_truth for q, _ in pairs) / total
    xs = [b["predicted"] for b in bins]
    ys = [b["actual"] for b in bins]
    if len(xs) > 1 and max(xs) > min(xs):
        mx, my = statistics.mean(xs), statistics.mean(ys)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum(
            (x - mx) ** 2 for x in xs
        )
    else:
        slope = None
    return bins, {
        "calibration_ece": round(ece, 4),
        "calibration_bias": round(mean_pred - mean_actual, 4),
        "calibration_slope": round(slope, 4) if slope is not None else None,
    }


def evaluate_dataset(
    ds: EstimateDataset,
    cfg: EstimateConfig,
    *,
    bar_min: int | None = None,
    bar_max: int | None = None,
) -> dict:
    """在 dataset 上逐日重放；``bar_min/bar_max`` 只切**查询**区间，不切样本。

    样本入库仍严格按 ``available_bar <= q.bar`` 推进，所以前半段选参时看不到
    后半段样本，后半段验证时可以用前半段历史 —— 这正是时间切分要的语义，
    也是网格防过拟合的基础（#122）。
    """
    cfg.validate()
    queries = [
        q for q in ds.queries
        if (bar_min is None or q.bar >= bar_min) and (bar_max is None or q.bar < bar_max)
    ]
    # 有校准器时必须从**历史最早处**重放全部查询：校准器的训练流完全靠 observe
    # 喂进来，若只遍历窗口内的查询，样本外评估就把它变成冷启动、抹掉已学到的
    # 映射（#123 初版正是这么把 platt 的样本外 skill 误判成负值的）。
    # 无校准器时估计量只由 ``advance_to`` 驱动，窗口外查询可直接跳过，等价且更快。
    replay_all = cfg.calibration != "none"
    est = TurnEstimator(cfg, ds.samples)
    pairs: list[tuple[_Query, TurnEstimate]] = []
    signals: list[tuple[_Query, TurnEstimate]] = []
    thresholds: list[float] = []
    for q in (ds.queries if replay_all else queries):
        if bar_max is not None and q.bar >= bar_max:
            break
        est.advance_to(q.bar)
        pred = est.estimate(q.code, q.key)
        if pred is None:
            continue
        # 把「当时给出的原始概率 + 事后标签」排队，等 available_bar 到了才允许
        # 进入校准器；这一句漏掉校准器就永远没有样本（samples=0），
        # 会静默退化成「没校准」——tests 里专门有一条锁这个。
        est.observe(q.available_bar, pred.p_raw, q.y_truth)
        if bar_min is not None and q.bar < bar_min:
            continue
        pairs.append((q, pred))
        # 基础率只取当时已入库样本；冷启动时 ≈0.5 → 阈值高 → 天然不发信号。
        # 绝不能用全样本的事后基础率，那等于把未来信息塞进发信号门槛。
        threshold = est.base_rate() * cfg.entry_lift
        thresholds.append(threshold)
        if pred.p_turn >= threshold and pred.exp_net_pct > cfg.min_exp_net:
            signals.append((q, pred))

    brier_vals = [(e.p_turn - q.y_truth) ** 2 for q, e in pairs]
    # Brier 必须跟「永远猜基础率」比：转折基础率不到 10% 时，0.08 左右的
    # Brier 看漂亮其实几乎不含信息量，只有 skill > 0 才算模型带来了增量。
    p0 = (sum(q.y_truth for q, _ in pairs) / len(pairs)) if pairs else 0.5
    const_vals = [(p0 - q.y_truth) ** 2 for q, _ in pairs]
    brier_score = round(sum(brier_vals) / len(brier_vals), 4) if brier_vals else None
    brier_base = round(sum(const_vals) / len(const_vals), 4) if const_vals else None
    brier_skill = (
        round(1.0 - brier_score / brier_base, 4)
        if brier_score is not None and brier_base else None
    )
    # 同一批重放同时算「校准前」的 Brier 与可靠性：#123 要回答的是校准有没有
    # 真的把低估修正，而不是只看校准后的数字变好看。
    raw_vals = [(e.p_raw - q.y_truth) ** 2 for q, e in pairs]
    brier_raw = round(sum(raw_vals) / len(raw_vals), 4) if raw_vals else None
    brier_skill_raw = (
        round(1.0 - brier_raw / brier_base, 4)
        if brier_raw is not None and brier_base else None
    )
    hits = sum(1 for q, _ in signals if q.y_truth == 1)
    bins, calib = _reliability(pairs)
    _, calib_raw = _reliability(pairs, "p_raw")

    ups = [q.forward_pct for q, _ in pairs if q.y_truth == 1 and q.forward_pct > 0]
    losses = [q.loss_pct for q, _ in pairs if q.y_truth == 0 and q.loss_pct > 0]
    confirmed = [q.confirmed_pct for q, _ in pairs if q.y_truth == 1 and q.confirmed_pct != 0]
    med_up, med_loss, med_conf = _median(ups), _median(losses), _median(confirmed)
    breakeven = None
    if med_up and med_loss and med_conf is not None and med_up + med_loss > 0:
        breakeven = round(
            (med_conf + med_loss + cfg.cost_pct) / (med_up + med_loss) * 100.0, 1
        )
    hit_rate = round(hits / len(signals), 4) if signals else None
    if len(signals) < 30 or breakeven is None:
        verdict = "insufficient_samples"
    elif hit_rate * 100.0 < breakeven:
        verdict = "below_breakeven"
    else:
        verdict = "above_breakeven"

    ps = sorted(e.p_turn for _, e in pairs)

    def _pct(p: float) -> float | None:
        return round(ps[min(len(ps) - 1, int(len(ps) * p))], 4) if ps else None

    recent = [
        {
            "code": q.code, "bar": q.bar, "p_turn": round(e.p_turn, 4),
            "exp_net_pct": round(e.exp_net_pct, 3), "y_truth": q.y_truth,
            "forward_pct": round(q.forward_pct, 3),
        }
        for q, e in signals[-20:]
    ]
    return {
        "config": {
            "horizon": cfg.horizon,
            "prior_strength": cfg.prior_strength,
            "symbol_cap": cfg.symbol_cap,
            "entry_lift": cfg.entry_lift,
            "cost_pct": cfg.cost_pct,
            "min_exp_net": cfg.min_exp_net,
            "z_window": cfg.z_window,
            "min_global_samples": cfg.min_global_samples,
            "features": cfg.features,
            "calibration": cfg.calibration,
            "calib_min_samples": cfg.calib_min_samples,
            "calib_refit_every": cfg.calib_refit_every,
            "calib_l2": cfg.calib_l2,
        },
        "bar_min": bar_min,
        "bar_max": bar_max,
        "samples": len(ds.samples),
        "queries": len(queries),
        "estimated_queries": len(pairs),
        "predicted_signals": len(signals),
        "hit_rate": hit_rate,
        "base_rate": round(sum(q.y_truth for q, _ in pairs) / len(pairs), 4) if pairs else None,
        "median_entry_threshold": _median(thresholds),
        "p_turn_p50": _pct(0.50),
        "p_turn_p90": _pct(0.90),
        "p_turn_p99": _pct(0.99),
        "brier_score": brier_score,
        "brier_score_raw": brier_raw,
        "brier_baseline": brier_base,
        "brier_skill": brier_skill,
        "brier_skill_raw": brier_skill_raw,
        "calibration_kind": "equal_frequency_deciles",
        "calibration_bins": bins,
        "calibration_ece": calib["calibration_ece"],
        "calibration_bias": calib["calibration_bias"],
        "calibration_slope": calib["calibration_slope"],
        # 校准前（原始分箱概率）的同一组指标：bias 收窄多少、slope 是否回到 1
        "calibration_before": calib_raw,
        "calibrator": est.calibrator_summary(),
        "calibrated_share": (
            round(sum(1 for _, e in pairs if e.calibrated) / len(pairs), 4) if pairs else None
        ),
        "median_upside_pct": med_up,
        "median_loss_pct": med_loss,
        "median_confirmed_pct": med_conf,
        "breakeven_hit_pct": breakeven,
        "verdict": verdict,
        "median_signal_forward_pct": _median([q.forward_pct for q, _ in signals]),
        "median_hit_forward_pct": _median([q.forward_pct for q, _ in signals if q.y_truth == 1]),
        "median_miss_loss_pct": _median([q.loss_pct for q, _ in signals if q.y_truth == 0]),
        "mean_realized_net_pct": (
            round(statistics.mean(
                [q.forward_pct - q.loss_pct - cfg.cost_pct for q, _ in signals]
            ), 3) if signals else None
        ),
        "median_exp_net_pct": _median([e.exp_net_pct for _, e in signals]),
        "median_global_samples": _median([float(e.global_samples) for _, e in pairs]),
        "median_symbol_weight": _median([e.symbol_weight for _, e in pairs]),
        "recent_signals": recent,
        "note": (
            "条件分布而非点位预测；标签窗口未走完的样本不入库；"
            "校准为等频十分箱（predicted<actual 即低估，与 exp_net 过度自信是两回事）；"
            "hit_rate 与 breakeven 均为近似口径，未接入评分/下单路径。"
        ),
    }


def evaluate(
    scans: list[SymbolScan],
    bench_close: pd.Series,
    cfg: EstimateConfig,
    *,
    features: str | None = None,
    bar_min: int | None = None,
    bar_max: int | None = None,
    dataset: EstimateDataset | None = None,
) -> dict:
    """便利包装：兼容 #121 的三参数调用，同时支持网格传入特征集 / 时间切分 / 复用 dataset。

    逐日重放整段，产出校准度、命中率、盈亏平衡判定，不产生任何下单动作。
    """
    if features is not None:
        cfg = replace(cfg, features=features)
    cfg.validate()
    ds = dataset if dataset is not None else build_dataset(scans, bench_close, cfg)
    return evaluate_dataset(ds, cfg, bar_min=bar_min, bar_max=bar_max)


#: 逐折返回的精简指标：与 ``_SLIM_KEYS``（网格）保持同一组口径，便于并排看。
FOLD_KEYS: tuple[str, ...] = (
    "estimated_queries", "predicted_signals", "hit_rate", "base_rate",
    "brier_score", "brier_score_raw", "brier_baseline", "brier_skill",
    "brier_skill_raw", "calibration_ece", "calibration_bias",
    "calibration_slope", "mean_realized_net_pct", "median_confirmed_pct",
    "breakeven_hit_pct", "verdict",
)


def _fold_stats(values: list[float]) -> dict:
    """逐折 skill 的均值 / 中位数 / 标准差 / 单边 t 与正态近似 p 值。

    只有 5 折时 t 分布尾部远厚于正态，这里如实标注 ``p_value_method``，
    结论一律要求「折间一致 + 过 Bonferroni 阈值」，不靠单个 p 值下判断。
    """
    k = len(values)
    if not k:
        return {
            "fold_count": 0, "skill_mean": None, "skill_median": None,
            "skill_sd": None, "positive_folds": 0, "consistency": None,
            "t_stat": None, "p_value_one_sided": None,
            "p_value_method": "normal_approx",
        }
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if k > 1 else 0.0
    t = mean / (sd / math.sqrt(k)) if sd > 0 and k > 1 else None
    p = 0.5 * math.erfc(t / math.sqrt(2.0)) if t is not None and t > 0 else (
        None if t is None else 1.0 - 0.5 * math.erfc(t / math.sqrt(2.0))
    )
    return {
        "fold_count": k,
        "skill_mean": round(mean, 4),
        "skill_median": round(statistics.median(values), 4),
        "skill_sd": round(sd, 4),
        "positive_folds": sum(1 for v in values if v > 0),
        "consistency": round(sum(1 for v in values if v > 0) / k, 3),
        "t_stat": round(t, 3) if t is not None else None,
        "p_value_one_sided": round(p, 6) if p is not None else None,
        "p_value_method": "normal_approx",
    }


def walk_forward_dataset(
    ds: EstimateDataset,
    cfg: EstimateConfig,
    *,
    folds: int = 5,
    bar_min: int | None = None,
    bar_max: int | None = None,
) -> dict:
    """滚动 walk-forward：把查询区间等频切成 ``folds`` 段，逐段独立评估。

    关键点：**每折都不是重新起跑**。``evaluate_dataset`` 始终从历史最早处推进
    估计器，所以第 i 折用的是「截至该折时点真实可见的全部样本」，只是指标只在
    该折的查询上统计 —— 这正是 walk-forward 的语义，也是 #122 单次 60/40 切分
    无法回答的问题：结论在时间上稳不稳。

    分折按**查询条数**等频而非按 bar 等宽：不同月份票的密度不同，等宽会让某折
    样本过少、skill 方差爆炸。边界若落在同一根 bar 内则后移，避免同一批查询
    被两折各算一次。
    """
    cfg.validate()
    if folds < 2:
        raise ValueError("walk-forward 至少需要 2 折")
    bars = [
        q.bar for q in ds.queries
        if (bar_min is None or q.bar >= bar_min) and (bar_max is None or q.bar < bar_max)
    ]
    if len(bars) < folds * 20:
        return {
            "folds": [], "fold_count": 0, "pooled": {},
            "note": "查询样本不足以分折（每折至少 20 条），不产出 walk-forward 结论",
        }

    total = len(bars)
    cuts: list[int] = []
    for i in range(1, folds):
        j = int(round(i * total / folds))
        while 0 < j < total and bars[j] == bars[j - 1]:
            j += 1
        if not cuts or j > cuts[-1]:
            cuts.append(j)
    edges = [0] + cuts + [total]

    fold_rows: list[dict] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if hi <= lo:
            continue
        row = evaluate_dataset(
            ds, cfg, bar_min=bars[lo], bar_max=None if hi >= total else bars[hi],
        )
        fold_rows.append({
            "fold": len(fold_rows) + 1,
            "bar_min": bars[lo],
            "bar_max": bars[hi - 1],
            **{k: row.get(k) for k in FOLD_KEYS},
        })

    pooled = evaluate_dataset(
        ds, cfg, bar_min=bar_min, bar_max=bar_max,
    )
    skills = [f["brier_skill"] for f in fold_rows if f["brier_skill"] is not None]
    stats = _fold_stats(skills)
    return {
        "folds": fold_rows,
        **stats,
        "pooled": {k: pooled.get(k) for k in ("p_turn_p99", "verdict", "calibrator", *FOLD_KEYS)},
        "note": (
            "逐折独立评估、估计器始终从历史最早处因果推进；"
            "brier_skill 相对各折自身基础率，跨折不可直接比大小，看符号一致性。"
        ),
    }


def adjust_multiple_tests(
    p_value: float | None,
    n_tests: int,
    *,
    alpha: float = 0.05,
    consistency: float | None = None,
    min_consistency: float = 0.6,
) -> dict:
    """Bonferroni 校正：把「网格里挑最好的那组」的选择偏差算进门槛。

    #122 的网格在开发期比较了 81 组配置，直接取最大 skill 一定包含选择性偏差；
    这里用最保守的 Bonferroni（``alpha / n_tests``）给出一个明确的准入线，
    同时要求折间方向一致（``consistency ≥ min_consistency``），避免「一折爆赚
    四折亏」被当成显著。校正不是万灵药：它只约束「至少有一组是真的」，
    不修正同一组在开发期被反复挑过的问题 —— 后者只能靠样本外只验一次来防。
    """
    n = max(1, int(n_tests))
    alpha_adj = alpha / n
    ok_consistency = consistency is None or consistency >= min_consistency
    significant = (
        p_value is not None and p_value < alpha_adj and ok_consistency
    )
    return {
        "n_tests": n,
        "alpha": alpha,
        "alpha_bonferroni": round(alpha_adj, 6),
        "p_value_one_sided": round(p_value, 6) if p_value is not None else None,
        "consistency": consistency,
        "min_consistency": min_consistency,
        "significant": bool(significant),
        "method": "bonferroni+fold_consistency",
    }
