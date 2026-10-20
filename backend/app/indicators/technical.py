"""技术指标计算（纯 Pandas 实现，每个指标附公式说明）。

约定：输入 DataFrame 需包含 open / high / low / close / volume 列
（均为前复权价格），按日期升序。指标列直接追加到 df 上返回。
"""
from __future__ import annotations

import pandas as pd


# ----------------------------------------------------------------------
# 均线族
# ----------------------------------------------------------------------
def ma(df: pd.DataFrame, periods: tuple[int, ...] = (5, 10, 20, 60)) -> pd.DataFrame:
    """移动平均线 MA（简单算术平均）。

    公式: MA(N) = (C_1 + C_2 + ... + C_N) / N
    其中 C_i 为最近第 i 日收盘价。常用 5/10/20/60 日线。
    """
    for n in periods:
        df[f"ma{n}"] = df["close"].rolling(window=n).mean()
    return df


def ema(series: pd.Series, n: int) -> pd.Series:
    """指数移动平均 EMA。

    公式: EMA_t = α × C_t + (1 - α) × EMA_{t-1},  α = 2 / (N + 1)
    α 为平滑系数，N 越大越平滑。递归起点用首个有效值填充。
    """
    return series.ewm(span=n, adjust=False).mean()


# ----------------------------------------------------------------------
# MACD
# ----------------------------------------------------------------------
def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD 指标（趋向指标）。

    推导过程:
      1) DIF（快线，异同移动平均）:
             DIF = EMA(12) - EMA(26)
         即短期与长期指数移动均线的差，反映快慢均线距离。
      2) DEA（慢线，信号线，DIF 的 9 日 EMA）:
             DEA = EMA_9(DIF)
      3) MACD 柱（红绿柱，国内软件常乘以 2 放大显示）:
             MACD柱 = 2 × (DIF - DEA)

    常见用法: DIF 上穿 DEA 为金叉（买入信号），下穿为死叉（卖出信号）。
    """
    ema_fast = ema(df["close"], fast)
    ema_slow = ema(df["close"], slow)
    df["dif"] = ema_fast - ema_slow          # 快线
    df["dea"] = df["dif"].ewm(span=signal, adjust=False).mean()  # 慢线
    df["macd_hist"] = 2 * (df["dif"] - df["dea"])  # 红绿柱
    return df


# ----------------------------------------------------------------------
# KDJ
# ----------------------------------------------------------------------
def kdj(df: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ 随机指标。

    推导过程:
      1) RSV（未成熟随机值）:
             RSV = (C - LLV(L, N)) / (HHV(H, N) - LLV(L, N)) × 100
         其中 LLV 为 N 日最低价，HHV 为 N 日最高价，C 为当日收盘价。
         RSV 衡量收盘价处于近 N 日振幅区间的相对位置（0=最低, 100=最高）。
      2) K 值 = SMA(RSV, M1)，即 RSV 的 M1 日移动平均（递推）:
             K_t = (M1 - 1) / M1 × K_{t-1} + 1 / M1 × RSV_t
      3) D 值 = SMA(K, M2):
             D_t = (M2 - 1) / M2 × D_{t-1} + 1 / M2 × K_t
      4) J 值 = 3 × K - 2 × D（J 对 K/D 的放大，可超出 [0, 100]）

    常见用法: J < 20 为超卖区，J > 80 为超买区；K 上穿 D 为金叉。
    默认参数 N=9, M1=M2=3。
    """
    low_n = df["low"].rolling(window=n).min()    # LLV
    high_n = df["high"].rolling(window=n).max()  # HHV
    rsv = (df["close"] - low_n) / (high_n - low_n) * 100
    # 处理一字板（最高=最低）导致的除零
    rsv = rsv.fillna(50.0)

    k = pd.Series(index=df.index, dtype=float)
    d = pd.Series(index=df.index, dtype=float)
    prev_k, prev_d = 50.0, 50.0  # 递推初始值（惯例取 50）
    for i in range(len(df)):
        if pd.isna(rsv.iloc[i]):
            k.iloc[i], d.iloc[i] = prev_k, prev_d
            continue
        prev_k = (m1 - 1) / m1 * prev_k + 1 / m1 * rsv.iloc[i]
        prev_d = (m2 - 1) / m2 * prev_d + 1 / m2 * prev_k
        k.iloc[i], d.iloc[i] = prev_k, prev_d
    df["kdj_k"], df["kdj_d"], df["kdj_j"] = k, d, 3 * k - 2 * d
    return df


# ----------------------------------------------------------------------
# BOLL
# ----------------------------------------------------------------------
def boll(df: pd.DataFrame, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    """布林带 BOLL。

    公式:
        中轨 MID = MA(N)（N 日收盘均线，默认 20）
        标准差  = sqrt( Σ(C_i - MID)² / N )
        上轨 UP = MID + k × 标准差      （默认 k=2）
        下轨 DN = MID - k × 标准差

    带宽随波动率自适应：波动大张口，波动小收口。
    常见用法: 价格触及下轨可能超卖反弹，突破上轨可能趋势启动。
    """
    mid = df["close"].rolling(window=n).mean()
    std = df["close"].rolling(window=n).std(ddof=0)  # 总体标准差
    df["boll_mid"] = mid
    df["boll_up"] = mid + k * std
    df["boll_dn"] = mid - k * std
    return df


# ----------------------------------------------------------------------
# RSI
# ----------------------------------------------------------------------
def rsi(df: pd.DataFrame, periods: tuple[int, ...] = (6, 12, 24)) -> pd.DataFrame:
    """相对强弱指标 RSI。

    公式（Wilder 平滑法）:
        Δ_t = C_t - C_{t-1}
        U_t = max(Δ_t, 0)      （涨幅）
        D_t = max(-Δ_t, 0)     （跌幅）
        RSI(N) = 100 × SMA_N(U) / (SMA_N(U) + SMA_N(D))

    RSI ∈ [0, 100]：>70 超买，<30 超卖，50 为多空分界。
    默认计算 6/12/24 日三个周期。
    """
    delta = df["close"].diff()
    gain = delta.clip(lower=0)   # 上涨幅度，跌时为 0
    loss = (-delta).clip(lower=0)  # 下跌幅度，涨时为 0
    for n in periods:
        avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
        # RS = 平均涨幅 / 平均跌幅；
        # 平均跌幅为 0（单边上涨）时 RS → +inf，RSI → 100，符合定义；
        # 两者皆 0（无波动）时 RS 为 NaN，RSI 记 50（中性）。
        rs = avg_gain / avg_loss
        rsi_n = 100 - 100 / (1 + rs)
        df[f"rsi{n}"] = rsi_n.fillna(50.0)
    return df


# ----------------------------------------------------------------------
# 乖离率
# ----------------------------------------------------------------------
def bias(df: pd.DataFrame, periods: tuple[int, ...] = (6, 12, 24)) -> pd.DataFrame:
    """乖离率 BIAS。

    公式: BIAS(N) = (C - MA(N)) / MA(N) × 100%
    含义: 收盘价偏离 N 日均线的百分比，衡量超买超卖程度。
    A股经验参考: 6日 BIAS > +6% 偏超买，< -6% 偏超卖（不同板块有差异）。
    """
    for n in periods:
        ma_n = df["close"].rolling(window=n).mean()
        df[f"bias{n}"] = (df["close"] - ma_n) / ma_n * 100
    return df


# ----------------------------------------------------------------------
# ATR（平均真实波幅）
# ----------------------------------------------------------------------
def atr(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """平均真实波幅 ATR，用于设定动态止损位。

    真实波幅 TR = max( H - L, |H - C_{t-1}|, |L - C_{t-1}| )
    （当日最高最低价差、最高价与前收差、最低价与前收差，三者取大）

    ATR(N) = Wilder 平滑: ATR_t = (ATR_{t-1} × (N-1) + TR_t) / N
    反映平均波动幅度，止损位常设为 入场价 - 2×ATR。
    """
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / n, adjust=False).mean()
    return df


# ----------------------------------------------------------------------
# 量比（需要成交量序列）
# ----------------------------------------------------------------------
def volume_ratio(df: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    """估算量比（基于日线近似，非盘中口径）。

    严格定义（盘中）: 当日累计成交量 / 已开盘分钟数 ÷ 过去5日平均每分钟成交量
    日线近似: 当日成交量 ÷ 过去 N 日平均成交量（假设日内分布均匀）。

    量比 > 1 表示放量，< 1 表示缩量。
    """
    avg_vol = df["volume"].rolling(window=n).mean()
    df["vol_ratio"] = df["volume"] / avg_vol
    return df


# ----------------------------------------------------------------------
# 汇总入口
# ----------------------------------------------------------------------
def add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为行情 DataFrame 追加全部常用指标（策略统一入口）。"""
    if df is None or df.empty:
        return df
    df = df.copy()
    df = ma(df)
    df = macd(df)
    df = kdj(df)
    df = boll(df)
    df = rsi(df)
    df = bias(df)
    df = atr(df)
    df = volume_ratio(df)
    return df
