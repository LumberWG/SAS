"""从 GitHub 上筛选出的、适合创业板短线的策略，统一接入本系统。

所有策略均遵循 Strategy 契约：evaluate(df) 只基于传入的前缀切片（无未来函数），
返回 Signal 或 None。指标在 evaluate 内自行计算（RSI14 / ADX / DMA / MA / 量MA），
并对异常做 try/except 兜底，保证任意参数组合下不崩溃。

策略来源（见对话）：
  - 量价共振2560 / RSI超跌反弹 / 均线多空MA2055 / MACD_DMA叠加
        -> github.com/PerCheung/ChinaAStockMarketRealtimeMonitor（创业板实时监测模型）
  - 创业板涨停后5日
        -> github.com/Conquerer-yinn/Kakaaaa-quant（创业板涨停后5日事件研究）
  - 底反ADX增强
        底部反转条件取自本系统内置「创业板底部反转」(app/strategies/builtin.py)；
        ADX 强度闸门(21<ADX<30 且方向上升、close(T-1)>MA20) 思路取自 github.com/ling-0729/KHunter
  - 动量T3
        -> github.com/sqyangx/quantlab（Model-A：T+1 买 / T+3 卖 纪律的简化代表）

这些策略与内置「创业板底部反转」共用同一套回测/扫描引擎与趋势滤网。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.strategies.base import Signal, Strategy
from app.strategies.registry import registry
from app.strategies.base import ParamSpec


# ----------------------------------------------------------------------
# 指标工具（在 evaluate 内基于前缀切片即时计算，避免未来函数）
# ----------------------------------------------------------------------
def _sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _cross_up(a: pd.Series, b: pd.Series) -> bool:
    """a 在最新一根上穿 b（且前一根未上穿）。"""
    if len(a) < 2 or len(b) < 2:
        return False
    return bool(a.iloc[-1] > b.iloc[-1] and a.iloc[-2] <= b.iloc[-2])


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    ag = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    al = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = ag / (al + 1e-12)
    return 100 - 100 / (1 + rs)


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """ADX 趋势强度（0~100，越大趋势越明确）。"""
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up.to_numpy(), 0.0)
    minus_dm = np.where((down > up) & (down > 0), down.to_numpy(), 0.0)
    prev_close = close.shift(1)
    tr = np.maximum.reduce([
        (high - low).abs().to_numpy(),
        (high - prev_close).abs().to_numpy(),
        (low - prev_close).abs().to_numpy(),
    ])
    tr_s = pd.Series(tr, index=close.index)
    atr = tr_s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = pd.Series(plus_dm, index=close.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / (atr + 1e-12) * 100
    minus_di = pd.Series(minus_dm, index=close.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / (atr + 1e-12) * 100
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-12)) * 100
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def _last_low(df: pd.DataFrame, lookback: int) -> float:
    return float(df["low"].astype(float).tail(max(1, lookback)).min())


# ----------------------------------------------------------------------
# 1) 量价共振2560（ChinaAStockMarketRealtimeMonitor 25-60 模型）
#    MA5 上穿 MA25 + VOL5 上穿 VOL60 双金叉
# ----------------------------------------------------------------------
@registry.register
class VolumePriceResonance2560(Strategy):
    name = "量价共振2560"
    description = "MA5上穿MA25 + VOL5上穿VOL60 双金叉（创业板实时监测 25-60 模型）"
    category = "动量"
    min_bars = 65

    parameters = {
        "ma_short": ParamSpec(type="int", default=5, label="短均线", min=3, max=20, step=1),
        "ma_long": ParamSpec(type="int", default=25, label="长均线", min=10, max=60, step=1),
        "vol_short": ParamSpec(type="int", default=5, label="短量线", min=3, max=20, step=1),
        "vol_long": ParamSpec(type="int", default=60, label="长量线", min=20, max=120, step=1),
        "target_pct": ParamSpec(type="float", default=0.06, label="止盈", min=0.02, max=0.3, step=0.01, unit="(比值)"),
        "stop_lookback": ParamSpec(type="int", default=10, label="止损回看", min=3, max=30, step=1, unit="日"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float)
            vol = df["volume"].astype(float)
            ma_s = _sma(close, int(self.param("ma_short")))
            ma_l = _sma(close, int(self.param("ma_long")))
            vol_s = _sma(vol, int(self.param("vol_short")))
            vol_l = _sma(vol, int(self.param("vol_long")))
            if not _cross_up(ma_s, ma_l):
                return None
            if not _cross_up(vol_s, vol_l):
                return None
            last = float(close.iloc[-1])
            stop = _last_low(df, int(self.param("stop_lookback")))
            target = round(last * (1 + float(self.param("target_pct"))), 2)
            return Signal(
                code="", name="", strategy=self.name, price=last,
                reason=f"MA{int(self.param('ma_short'))}金叉MA{int(self.param('ma_long'))}+VOL金叉",
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
            )
        except Exception:
            return None


# ----------------------------------------------------------------------
# 2) RSI超跌反弹（ChinaAStockMarketRealtimeMonitor RSI14-70 模型）
#    RSI14 近期跌破超卖线后拐头向上
# ----------------------------------------------------------------------
@registry.register
class RSIOversoldRebound(Strategy):
    name = "RSI超跌反弹"
    description = "RSI14 跌破超卖线后拐头向上（创业板实时监测 RSI14-70 模型）"
    category = "反转"
    min_bars = 60

    parameters = {
        "rsi_n": ParamSpec(type="int", default=14, label="RSI周期", min=6, max=30, step=1),
        "oversold": ParamSpec(type="float", default=35.0, label="超卖阈值", min=20.0, max=50.0, step=1.0),
        "target_pct": ParamSpec(type="float", default=0.04, label="止盈", min=0.02, max=0.2, step=0.01, unit="(比值)"),
        "stop_lookback": ParamSpec(type="int", default=8, label="止损回看", min=3, max=30, step=1, unit="日"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float)
            rsi = _rsi(close, int(self.param("rsi_n")))
            if len(rsi) < 4:
                return None
            r = float(rsi.iloc[-1])
            rp = float(rsi.iloc[-2])
            ov = float(self.param("oversold"))
            if r <= rp:                      # 需拐头向上
                return None
            if float(rsi.tail(5).min()) > ov:   # 近 5 日须触及过超卖
                return None
            if r > ov + 8:                  # 已远离超卖，错过买点
                return None
            last = float(close.iloc[-1])
            stop = _last_low(df, int(self.param("stop_lookback")))
            target = round(last * (1 + float(self.param("target_pct"))), 2)
            return Signal(
                code="", name="", strategy=self.name, price=last,
                reason=f"RSI{int(self.param('rsi_n'))}={r:.1f} 超卖拐头",
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
            )
        except Exception:
            return None


# ----------------------------------------------------------------------
# 3) 均线多空MA2055（ChinaAStockMarketRealtimeMonitor MA20-55 模型）
#    MA20>MA55 多头排列 + 价格在MA20上 + MA20上行
# ----------------------------------------------------------------------
@registry.register
class MA2055Trend(Strategy):
    name = "均线多空MA2055"
    description = "MA20>MA55 多头排列且价在MA20上、MA20上行（创业板实时监测 MA20-55 模型）"
    category = "趋势"
    min_bars = 60

    parameters = {
        "ma_short": ParamSpec(type="int", default=20, label="短均线", min=5, max=60, step=1),
        "ma_long": ParamSpec(type="int", default=55, label="长均线", min=30, max=120, step=1),
        "target_pct": ParamSpec(type="float", default=0.12, label="止盈", min=0.05, max=0.4, step=0.01, unit="(比值)"),
        "stop_lookback": ParamSpec(type="int", default=12, label="止损回看", min=3, max=30, step=1, unit="日"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float)
            ms = _sma(close, int(self.param("ma_short")))
            ml = _sma(close, int(self.param("ma_long")))
            if len(ms) < 6:
                return None
            last = float(close.iloc[-1])
            if not (float(ms.iloc[-1]) > float(ml.iloc[-1])):
                return None
            if not (last > float(ms.iloc[-1])):
                return None
            if not (float(ms.iloc[-1]) > float(ms.iloc[-6])):   # MA20 上行
                return None
            stop = _last_low(df, int(self.param("stop_lookback")))
            target = round(last * (1 + float(self.param("target_pct"))), 2)
            return Signal(
                code="", name="", strategy=self.name, price=last,
                reason=f"MA{int(self.param('ma_short'))}>MA{int(self.param('ma_long'))} 多头排列",
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
            )
        except Exception:
            return None


# ----------------------------------------------------------------------
# 4) MACD+DMA 叠加（ChinaAStockMarketRealtimeMonitor MACD+DMA 模型）
#    MACD 金叉(dif>dea) + DMA 金叉(DIF=MA10-MA50, AMA=MA(DIF,10))
# ----------------------------------------------------------------------
@registry.register
class MACDDMA(Strategy):
    name = "MACD_DMA叠加"
    description = "MACD金叉(dif>dea) + DMA金叉(DIF=MA10-MA50, AMA=MA(DIF,10))（创业板实时监测 MACD+DMA 模型）"
    category = "趋势"
    min_bars = 60

    parameters = {
        "target_pct": ParamSpec(type="float", default=0.10, label="止盈", min=0.03, max=0.4, step=0.01, unit="(比值)"),
        "stop_lookback": ParamSpec(type="int", default=12, label="止损回看", min=3, max=30, step=1, unit="日"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float)
            ema12 = _ema(close, 12)
            ema26 = _ema(close, 26)
            dif = ema12 - ema26
            dea = _ema(dif, 9)
            if not _cross_up(dif, dea):
                return None
            dma_dif = _sma(close, 10) - _sma(close, 50)
            dma_ama = _sma(dma_dif, 10)
            if not _cross_up(dma_dif, dma_ama):
                return None
            last = float(close.iloc[-1])
            stop = _last_low(df, int(self.param("stop_lookback")))
            target = round(last * (1 + float(self.param("target_pct"))), 2)
            return Signal(
                code="", name="", strategy=self.name, price=last,
                reason="MACD金叉+DMA金叉",
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
            )
        except Exception:
            return None


# ----------------------------------------------------------------------
# 5) 创业板涨停后5日（Kakaaaa-quant 创业板涨停后5日事件研究）
#    识别 300/301 涨停(≥19.5% 且封板)，次日买入，持有 5 日（回测用 max_hold_days=5）
# ----------------------------------------------------------------------
@registry.register
class ChiNextLimitUp5d(Strategy):
    name = "创业板涨停后5日"
    description = "识别300/301涨停(≥19.5%且封板)，次日买入持有5日（Kakaaaa-quant 创业板涨停后5日事件研究）"
    category = "事件"
    min_bars = 60

    parameters = {
        "limit_th": ParamSpec(type="float", default=0.195, label="涨停阈值", min=0.18, max=0.21, step=0.005, unit="(涨幅)"),
        "target_pct": ParamSpec(type="float", default=0.08, label="止盈", min=0.0, max=0.3, step=0.01, unit="(比值)"),
        "stop_pct": ParamSpec(type="float", default=0.08, label="止损", min=0.0, max=0.2, step=0.01, unit="(比值)"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float)
            high = df["high"].astype(float)
            pct = close / close.shift(1) - 1.0
            if len(pct) < 3:
                return None
            limit_th = float(self.param("limit_th"))
            y_pct = float(pct.iloc[-2])
            y_high = float(high.iloc[-2])
            y_close = float(close.iloc[-2])
            # 昨天涨停且封板（最高≈收盘，未被打开）
            y_is_limit = (y_pct >= limit_th) and (abs(y_high - y_close) < 0.01 * y_close)
            if not y_is_limit:
                return None
            last = float(close.iloc[-1])
            stop = round(last * (1 - float(self.param("stop_pct"))), 2)
            target = round(last * (1 + float(self.param("target_pct"))), 2)
            return Signal(
                code="", name="", strategy=self.name, price=last,
                reason="昨日创业板涨停，次日买入",
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
            )
        except Exception:
            return None


# ----------------------------------------------------------------------
# 6) 底反ADX增强（KHunter 底部反转 + ADX 强度闸门思路）
#    复用内置「创业板底部反转」的低位+回撤+换手+成交条件，外加 ADX≥阈值
#    才放行，过滤无趋势的震荡接飞刀。
# ----------------------------------------------------------------------
@registry.register
class BottomReversalADX(Strategy):
    name = "底反ADX增强"
    description = "创业板底部反转原条件 + ADX趋势强度闸门(≥阈值才放，减少接飞刀)（KHunter思路）"
    category = "反转"
    min_bars = 130

    parameters = {
        "start_pos_max": ParamSpec(type="float", default=0.40, label="起点位置上限", min=0.0, max=1.0, step=0.05, unit="(0=最低,1=最高)"),
        "dd20_min": ParamSpec(type="float", default=0.15, label="20日回撤下限", min=0.0, max=0.8, step=0.01, unit="(≥此值才算跌够)"),
        "turnover_min": ParamSpec(type="float", default=5.0, label="换手下限", min=0.0, max=30.0, step=0.5, unit="(百分制,5.0=5%)"),
        "min_avg_amount": ParamSpec(type="float", default=50_000_000.0, label="20日均额下限", min=0.0, max=2_000_000_000.0, step=10_000_000.0, unit="元"),
        "adx_lo": ParamSpec(type="float", default=21.0, label="ADX下界", min=0.0, max=50.0, step=1.0, unit="(21<ADX)"),
        "adx_hi": ParamSpec(type="float", default=30.0, label="ADX上界", min=0.0, max=60.0, step=1.0, unit="(ADX<30)"),
        "target_pct": ParamSpec(type="float", default=0.25, label="止盈", min=0.05, max=2.0, step=0.05, unit="(比值)"),
        "stop_lookback": ParamSpec(type="int", default=12, label="止损回看", min=3, max=60, step=1, unit="日"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float).to_numpy()
            if len(close) < 121:
                return None
            last = float(close[-1])
            base20 = float(close[-21])
            if base20 <= 0:
                return None
            win = close[-120:]
            lmin = float(win.min())
            hmax = float(win.max())
            start_pos = 0.5 if hmax == lmin else (base20 - lmin) / (hmax - lmin)
            roll_max20 = float(win[-20:].max())
            if roll_max20 <= 0:
                return None
            dd20 = 1.0 - last / roll_max20
            turnover = float(df["turnover_rate"].astype(float).to_numpy()[-1]) if "turnover_rate" in df else 0.0
            amt = df["amount"].astype(float).to_numpy()[-20:]
            avg_amount20 = float(amt.mean()) if len(amt) else 0.0

            if start_pos > float(self.param("start_pos_max")):
                return None
            if dd20 < float(self.param("dd20_min")):
                return None
            if turnover < float(self.param("turnover_min")):
                return None
            if avg_amount20 < float(self.param("min_avg_amount")):
                return None

            # ADX 闸门（KHunter 口径）：21<ADX(T-1)<30 且方向上升，close(T-1)>MA20
            adx_s = _adx(df["high"].astype(float), df["low"].astype(float),
                        df["close"].astype(float), 14)
            if len(adx_s) < 4:
                return None
            adx_t1 = float(adx_s.iloc[-2])          # T-1 根（防前视）
            if not (float(self.param("adx_lo")) < adx_t1 < float(self.param("adx_hi"))):
                return None
            if not (float(adx_s.iloc[-2]) > float(adx_s.iloc[-3]) > float(adx_s.iloc[-4])):
                return None                          # 方向须连续上升
            ma20 = _sma(df["close"].astype(float), 20)
            if float(df["close"].iloc[-2]) <= float(ma20.iloc[-2]):
                return None                          # 首仓闸门：价在 MA20 上

            lastp = float(df["close"].astype(float).iloc[-1])
            stop = _last_low(df, int(self.param("stop_lookback")))
            target = round(lastp * (1 + float(self.param("target_pct"))), 2)
            return Signal(
                code="", name="", strategy=self.name, price=lastp,
                reason=f"底反+ADX={adx_t1:.1f}",
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
            )
        except Exception:
            return None


# ----------------------------------------------------------------------
# 7) 动量T3（quantlab Model-A 纪律近似）
#    入场：3 日动量上行 + 放量（注：源仓 Model-A 实际是 Kronos 模型分 + LLM 重排，
#          此处动量阈值为结构化代理信号，并非源仓库参数）。
#    出场：硬 T+3 纪律（T+1 开盘买、信号日+2 交易日收盘卖 = 买后约 2 交易日，
#          由回测 max_hold_days 控制，不依赖止盈/止损）。
#    过滤：信号日收盘已涨停则跳过（对应源仓 drop_signal_limit_up）。
# ----------------------------------------------------------------------
@registry.register
class MomentumT3(Strategy):
    name = "动量T3"
    description = "3日动量上行+放量(代理入场)；T+1买/T+3硬出场(固定持有)；信号日涨停则跳过（Model-A 纪律近似）"
    category = "动量"
    min_bars = 30

    parameters = {
        "mom_days": ParamSpec(type="int", default=3, label="动量窗口", min=2, max=10, step=1, unit="日"),
        "mom_min": ParamSpec(type="float", default=0.03, label="最小涨幅", min=0.0, max=0.2, step=0.01, unit="(比值)"),
        "vol_mult": ParamSpec(type="float", default=1.2, label="放量倍数", min=1.0, max=3.0, step=0.1, unit="(×5日均量)"),
        "hold_days": ParamSpec(type="int", default=2, label="T+3硬持有", min=1, max=10, step=1, unit="交易日(买后)"),
        "target_pct": ParamSpec(type="float", default=0.0, label="止盈(0关)", min=0.0, max=0.3, step=0.01, unit="(比值)"),
        "stop_pct": ParamSpec(type="float", default=0.0, label="止损(0关)", min=0.0, max=0.3, step=0.01, unit="(比值)"),
        "skip_limit_up": ParamSpec(type="bool", default=True, label="跳过涨停"),
    }

    def evaluate(self, df: pd.DataFrame) -> Signal | None:
        if len(df) < self.min_bars:
            return None
        try:
            close = df["close"].astype(float)
            vol = df["volume"].astype(float)
            n = int(self.param("mom_days"))
            if len(close) <= n + 1:
                return None
            # 跳过涨停：信号日收盘相对前收已达涨停（源仓 drop_signal_limit_up 近似，无前视）
            if bool(self.param("skip_limit_up")):
                code = str(df.attrs.get("code", ""))
                pre = code[2:4] if len(code) >= 6 else ""
                thr = 0.198 if pre in ("30", "68") else 0.098   # 创业/科创 20% 缓冲2bp；其余 10%
                sig_ret = float(close.iloc[-1] / close.iloc[-2] - 1.0)
                if sig_ret >= thr:
                    return None
            mom = float(close.iloc[-1] / close.iloc[-1 - n] - 1.0)
            if mom < float(self.param("mom_min")):
                return None
            vol5 = _sma(vol, 5)
            if float(vol.iloc[-1]) < float(vol5.iloc[-1]) * float(self.param("vol_mult")):
                return None
            last = float(close.iloc[-1])
            # 硬 T+3 出场：默认关闭止盈/止损，由回测 max_hold_days 控制卖点
            tp = float(self.param("target_pct"))
            sp = float(self.param("stop_pct"))
            target = round(last * (1 + tp), 2) if tp > 0 else None
            stop = round(last * (1 - sp), 2) if sp > 0 else None
            skip = "跳过涨停" if bool(self.param("skip_limit_up")) else ""
            return Signal(
                code="", name="", strategy=self.name, price=last,
                reason=f"{n}日动量{mom*100:.1f}%放量 {skip}".strip(),
                stop_loss=stop, target_price=target, date=str(df["date"].iloc[-1]),
                extra={"hold_days": int(self.param("hold_days"))},
            )
        except Exception:
            return None
