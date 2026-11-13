"""运行时滚动 ML-IC 门控（研究专用：IC 门控 ML 叠加 / 第三条路 v2）。

思路
----
因子为底，仅当「滚动已实现 rank-IC」足够高时才用 ML 打分（percentile 0~100），
否则回退 14 因子。滚动 IC 用「预测样本」与其「向前 horizon 日收益」计算，
只用 ``pred_date + horizon <= today`` 已实现的样本（零未来函数）。

与 regime-aware 的本质区别
-------------------------
regime-aware 用「价格趋势(MA/波动)」路由，而 ML 真正有效的开关是「ML-IC(模型
置信度)」——二者不相关甚至负相关（见 forward_validation_dashboard 洞察④）。本模块
直接门控在 ML-IC 上，正是 oracle 显示能翻正到 +2pp 的轴。

约定
----
- ``ml_pred[(code, date)]`` 是 0~100 的截面分位（越高越看好）。
- 向前收益 ``fwd_ret = close[date+horizon] / close[date] - 1``。
- 滚动窗口用「已实现的 (ml_score, fwd_ret) 样本数」近似（默认 400 对），计算秩相关 IC。
"""
from __future__ import annotations

import bisect
import math
from collections import deque


def _ranks(vals: list[float]) -> list[float]:
    """平均秩（平局取均值），与 scipy 行为一致。"""
    s = sorted(range(len(vals)), key=lambda i: vals[i])
    r = [0.0] * len(vals)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and vals[s[j + 1]] == vals[s[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[s[k]] = avg
        i = j + 1
    return r


def _rank_ic(xs: list[float], ys: list[float]) -> float:
    """Pearson of ranks = Spearman rank-IC。"""
    n = len(xs)
    if n < 2:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in rx))
    dy = math.sqrt(sum((v - my) ** 2 for v in ry))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


class ICGate:
    """运行时滚动已实现 IC 门控。

    在回测主循环里，每个 (code, date) 候选都先 ``record`` 其 ML 分（不论当天是否采用），
    再 ``decide(today)`` 计算截至 today 的滚动 IC 并返回是否启用 ML。决策按天缓存。
    """

    # 模块级遥测（单进程内跨段共享，供研究脚本读取；生产不用）
    TELEMETRY: dict = {"use_days": 0, "total_days": 0, "ic_sum": 0.0, "ic_n": 0}

    @classmethod
    def reset_telemetry(cls) -> None:
        cls.TELEMETRY = {"use_days": 0, "total_days": 0, "ic_sum": 0.0, "ic_n": 0}

    def __init__(
        self,
        klines: dict,
        horizon: int = 15,
        window: int = 400,
        thresh: float = 0.12,
        min_samples: int = 20,
    ):
        self.horizon = int(horizon)
        self.window = int(window)
        self.thresh = float(thresh)
        self.min_samples = int(min_samples)
        # code -> (sorted dates, date->index, date->close)
        self._idx: dict[str, tuple[list[str], dict[str, int], dict[str, float]]] = {}
        for code, df in (klines or {}).items():
            if df is None or getattr(df, "empty", True):
                continue
            dates = [str(r.date) for r in df.itertuples(index=False)]
            if not dates:
                continue
            dmap = {d: i for i, d in enumerate(dates)}
            cmap = {str(r.date): float(r.close) for r in df.itertuples(index=False)}
            self._idx[code] = (dates, dmap, cmap)
        self._pending: dict[tuple[str, str], float] = {}
        self._realized: deque = deque()  # (pred_date, ml_score, fwd_ret)
        self._cache_date: str | None = None
        self._cache_use: bool = False
        self.ic: float = 0.0  # 最近一次计算的滚动 IC（供日志/审计）

    def record(self, code: str, date: str, ml_score: float) -> None:
        self._pending[(code, date)] = float(ml_score)

    def _fwd_ret(self, code: str, date: str):
        info = self._idx.get(code)
        if info is None:
            return None
        dates, dmap, cmap = info
        i = dmap.get(date)
        if i is None:
            return None
        j = i + self.horizon
        if j >= len(dates):
            return None
        c0 = cmap.get(date)
        c1 = cmap.get(dates[j])
        if not c0 or c0 <= 0 or c1 is None:
            return None
        return c1 / c0 - 1.0

    def _flush(self, today: str) -> None:
        """把截至 today 已实现的预测样本移入滚动缓冲。"""
        for (code, date), score in list(self._pending.items()):
            info = self._idx.get(code)
            if info is None:
                self._pending.pop((code, date), None)
                continue
            dates, dmap, _ = info
            i = dmap.get(date)
            if i is None:
                self._pending.pop((code, date), None)
                continue
            j = i + self.horizon
            if j < len(dates) and dates[j] <= today:
                fr = self._fwd_ret(code, date)
                if fr is not None:
                    self._realized.append((date, score, fr))
                self._pending.pop((code, date), None)
        # 滚动窗口：仅保留最近 window 对
        while len(self._realized) > self.window:
            self._realized.popleft()

    def decide(self, today: str) -> bool:
        """计算截至 today 的滚动 IC，返回是否启用 ML（按天缓存）。"""
        if self._cache_date == today:
            return self._cache_use
        self._cache_date = today
        self._flush(today)
        t = ICGate.TELEMETRY
        t["total_days"] += 1
        if len(self._realized) < self.min_samples:
            self.ic = 0.0
            self._cache_use = False
            return False
        xs = [p[1] for p in self._realized]
        ys = [p[2] for p in self._realized]
        ic = _rank_ic(xs, ys)
        self.ic = ic
        self._cache_use = ic >= self.thresh
        t["ic_sum"] += ic
        t["ic_n"] += 1
        if self._cache_use:
            t["use_days"] += 1
        return self._cache_use
