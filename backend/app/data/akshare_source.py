"""akshare 数据源实现。

底层依赖免费开源库 akshare，主要接口（东方财富 / 新浪财经）：
- 股票池    : stock_info_a_code_name
- 实时快照  : stock_zh_a_spot_em
- 日线历史  : stock_zh_a_hist（支持前/后复权）
- 指数快照  : stock_zh_index_spot_em
- 指数日线  : stock_zh_index_daily_em

所有网络调用带重试与超时，单次失败不影响上层整体流程。
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from app.config import get_settings
from app.data.base import BaseDataSource
from app.data.fallback import code_to_symbol, sina_kline, sina_quotes, tencent_kline
from app.net import apply_data_network_settings

logger = logging.getLogger("sas.data.akshare")

#: 默认拉取的K线起始长度（自然日，约3年，足够计算各类中长期指标）
_DEFAULT_HISTORY_DAYS = 1100


class DataSourceError(Exception):
    """数据源异常。"""


class CircuitBreaker:
    """简单熔断器：某接口连续失败达到阈值后，进入冷却期直接跳过。

    场景：部分网络环境下东方财富主机完全不可达，若每只股票都重试
    3 次再降级，5000 只股票的扫描将浪费大量时间在必然失败的请求上。
    熔断后冷却期内直接走降级源，冷却期结束自动恢复试探。
    """

    def __init__(self, threshold: int = 3, cooldown: float = 300.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, key: str) -> bool:
        """冷却期内返回 True（应跳过该接口）。"""
        return time.monotonic() < self._open_until.get(key, 0.0)

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._open_until.pop(key, None)

    def record_failure(self, key: str) -> None:
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= self.threshold:
            self._open_until[key] = time.monotonic() + self.cooldown
            logger.warning(
                "接口 %s 连续失败 %d 次，熔断 %d 秒", key, count, self.cooldown
            )


def _market_of(code: str) -> str:
    """根据代码前缀判断交易市场。

    - 60xxxx  → 上交所 sh
    - 00/30xx → 深交所 sz
    - 4/8/92x → 北交所 bj
    """
    if code.startswith(("60", "68", "90")):
        return "sh"
    if code.startswith(("00", "30", "20")):
        return "sz"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sh"


def _normalize_date(value: str) -> str:
    """日期归一化为 YYYY-MM-DD（兼容 YYYYMMDD 输入）。"""
    value = str(value).replace("-", "").replace("/", "")
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value


class AkshareDataSource(BaseDataSource):
    """akshare 数据源。"""

    name = "akshare"

    def __init__(self) -> None:
        self.settings = get_settings()
        # 确保国内数据源直连（绕过不可用的系统代理）
        apply_data_network_settings(
            domains=self.settings.data_no_proxy_domains,
            enabled=self.settings.data_direct_only,
        )
        self._breaker = CircuitBreaker()
        import akshare as ak  # 延迟导入，加快应用启动

        self._ak = ak

    # ------------------------------------------------------------------
    # 内部工具：带重试与熔断的调用
    # ------------------------------------------------------------------
    def _call(self, func_name: str, retries: int | None = None, timeout: float = 20.0, **kwargs):
        """调用 akshare 接口：熔断优先，失败重试（线性退避）。

        关键：akshare 底层 requests 不自带超时，网络不通时会挂到 TCP 超时
        （可达分钟级），叠加重试会把单次调用拖成十几分钟，且若在事件循环
        线程内同步调用会直接堵死整个后端。这里用线程池 + ``fut.result(timeout)``
        给每次调用硬上限，超时即视为失败并快速降级。
        """
        if self._breaker.is_open(func_name):
            # 熔断冷却期内直接失败，交由上层降级
            raise DataSourceError(f"akshare.{func_name} 已熔断，直接降级")

        retries = retries if retries is not None else self.settings.fetch_retries
        func = getattr(self._ak, func_name)
        last_err: Exception | None = None
        for attempt in range(1, retries + 2):
            if self._breaker.is_open(func_name):
                raise DataSourceError(f"akshare.{func_name} 已熔断，直接降级")
            ex = _cf.ThreadPoolExecutor(max_workers=1)
            try:
                _fut = ex.submit(func, **kwargs)
                result = _fut.result(timeout=timeout)
                self._breaker.record_success(func_name)
                return result
            except _cf.TimeoutError:
                # 超时即放弃：不等待仍在跑 akshare 的泄漏线程（wait=False），
                # 否则 shutdown 会 join 该线程（可能卡到 TCP 超时，几分钟）
                ex.shutdown(wait=False)
                last_err = TimeoutError(f"akshare.{func_name} 超时({timeout}s)")
                logger.warning(
                    "akshare.%s 第 %d 次调用超时(%ds)", func_name, attempt, int(timeout)
                )
                if attempt <= retries:
                    time.sleep(0.5 * attempt)  # 线性退避
            except DataSourceError:
                ex.shutdown(wait=False)
                raise
            except Exception as exc:  # noqa: BLE001 - 数据源异常统一兜底
                ex.shutdown(wait=False)
                last_err = exc
                logger.warning(
                    "akshare.%s 第 %d 次调用失败: %s", func_name, attempt, exc
                )
                if attempt <= retries:
                    time.sleep(0.5 * attempt)  # 线性退避
        self._breaker.record_failure(func_name)
        raise DataSourceError(f"akshare.{func_name} 调用失败: {last_err}") from last_err

    # ------------------------------------------------------------------
    # 股票池
    # ------------------------------------------------------------------
    def get_stock_pool(self) -> pd.DataFrame:
        """全A股股票池（含沪深京，剔除退市）。"""
        df = self._call("stock_info_a_code_name")
        df = df.rename(columns={"code": "code", "name": "name"})
        df["code"] = df["code"].astype(str).str.zfill(6)
        df["market"] = df["code"].map(_market_of)
        return df[["code", "name", "market"]]

    # ------------------------------------------------------------------
    # 实时快照
    # ------------------------------------------------------------------
    def get_realtime_snapshot(self) -> pd.DataFrame:
        """全市场实时快照。

        主源：东方财富。若其不可用（部分网络屏蔽 push2 主机），
        降级为新浪批量实时行情（成交量口径统一为手）。
        """
        try:
            df = self._call("stock_zh_a_spot_em")
        except DataSourceError:
            logger.warning("东方财富快照不可用，降级为新浪批量实时行情")
            pool = self.get_stock_pool()
            symbols = [
                code_to_symbol(r.code, r.market) for r in pool.itertuples()
            ]
            df = sina_quotes(symbols)
            if df.empty:
                raise DataSourceError("所有数据源均不可用（实时快照）") from None
            return df.reset_index(drop=True)

        df = df.rename(columns={
            "代码": "code", "名称": "name", "最新价": "price",
            "涨跌幅": "pct_change", "涨跌额": "change", "成交量": "volume",
            "成交额": "amount", "最高": "high", "最低": "low", "今开": "open",
            "昨收": "prev_close", "量比": "volume_ratio", "换手率": "turnover_rate",
            "总市值": "total_mv", "流通市值": "float_mv",
        })
        keep = [
            "code", "name", "price", "pct_change", "change", "volume", "amount",
            "high", "low", "open", "prev_close", "volume_ratio", "turnover_rate",
            "total_mv", "float_mv",
        ]
        keep = [c for c in keep if c in df.columns]
        df = df[keep].copy()
        df["code"] = df["code"].astype(str).str.zfill(6)
        # 数值列统一转 float，脏数据置 NaN
        for col in ("price", "pct_change", "volume", "amount", "turnover_rate",
                    "volume_ratio", "high", "low", "open", "prev_close"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # 剔除停牌（最新价为 NaN）的股票
        df = df[df["price"].notna()]
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # 日线历史
    # ------------------------------------------------------------------
    def get_daily_kline(
        self, code: str, adjust: str = "qfq", start_date: str | None = None,
        end_date: str | None = None, timeout: float | None = None,
        retries: int | None = None,
    ) -> pd.DataFrame:
        """单只股票日线历史。

        注意：akshare 的 stock_zh_a_hist 只返回区间数据，
        默认起始日期取当前往前约3年（约1100自然日）。
        """
        today = datetime.now()
        if end_date is None:
            end_date = today.strftime("%Y%m%d")
        if start_date is None:
            start = today - timedelta(days=_DEFAULT_HISTORY_DAYS)
            start_date = start.strftime("%Y%m%d")

        # start_date / end_date 统一为 YYYY-MM-DD 便于后续比较
        start_norm = _normalize_date(start_date)
        end_norm = _normalize_date(end_date)

        try:
            df = self._call(
                "stock_zh_a_hist",
                symbol=code, period="daily",
                start_date=start_norm.replace("-", ""),
                end_date=end_norm.replace("-", ""),
                adjust=adjust,
                timeout=timeout, retries=retries,
            )
        except DataSourceError:
            # 降级链：腾讯K线 → 新浪K线（均忽略原始日期参数，拉取后自行过滤）
            return self._kline_from_fallback(code, adjust, start_norm, end_norm)

        if df is None or df.empty:
            return pd.DataFrame(
                columns=["date", "open", "close", "high", "low", "volume",
                         "amount", "pct_change", "turnover_rate"]
            )
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount",
            "涨跌幅": "pct_change", "换手率": "turnover_rate",
        })
        keep = ["date", "open", "close", "high", "low", "volume", "amount",
                "pct_change", "turnover_rate"]
        keep = [c for c in keep if c in df.columns]
        df = df[keep].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in ("open", "close", "high", "low", "volume", "amount",
                    "pct_change", "turnover_rate"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return df

    # ------------------------------------------------------------------
    # 降级K线链
    # ------------------------------------------------------------------
    def _kline_from_fallback(
        self, code: str, adjust: str, start_norm: str, end_norm: str
    ) -> pd.DataFrame:
        """K线降级链：腾讯 → 新浪。

        两个备用源均忽略调用方传入的日期参数（按固定根数拉取），
        拉取后再按 [start_norm, end_norm] 过滤。
        注意：新浪源不支持复权参数，返回其默认口径。
        """
        try:
            logger.warning("东方财富K线不可用，降级为腾讯K线 code=%s", code)
            df = tencent_kline(code, adjust=adjust)
        except Exception as exc:  # noqa: BLE001 - 腾讯也可能触发风控
            logger.warning("腾讯K线不可用(%s)，降级为新浪K线 code=%s", exc, code)
            df = sina_kline(code_to_symbol(code), datalen=300)
        if df.empty:
            return df
        return df[(df["date"] >= start_norm) & (df["date"] <= end_norm)].reset_index(
            drop=True
        )

    # ------------------------------------------------------------------
    # 指数
    # ------------------------------------------------------------------
    #: 主要指数列表（展示名 → (新浪代码, 东财代码)）
    MAJOR_INDICES: dict[str, tuple[str, str]] = {
        "上证指数": ("sh000001", "000001"),
        "深证成指": ("sz399001", "399001"),
        "创业板指": ("sz399006", "399006"),
        "科创50":   ("sh000688", "000688"),
        "沪深300":  ("sh000300", "000300"),
        "中证500":  ("sh000905", "000905"),
    }

    def get_index_snapshot(self) -> pd.DataFrame:
        """主要指数实时快照。主源东方财富，失败降级新浪。"""
        try:
            df = self._call("stock_zh_index_spot_em", symbol="沪深重要指数")
        except DataSourceError:
            logger.warning("东方财富指数快照不可用，降级为新浪实时行情")
            symbols = [v[0] for v in self.MAJOR_INDICES.values()]
            df = sina_quotes(symbols)
            if df.empty:
                raise DataSourceError("所有数据源均不可用（指数快照）") from None
            df["symbol"] = df["code"].map(
                lambda c: f"sh{c}" if c.startswith("0") else f"sz{c}"
            )
            df["em_code"] = df["code"]
            keep = ["symbol", "em_code", "name", "price", "change", "pct_change",
                    "volume", "amount"]
            df = df[[c for c in keep if c in df.columns]].copy()
            return df.reset_index(drop=True)

        df = df.rename(columns={
            "代码": "em_code", "名称": "name", "最新价": "price",
            "涨跌额": "change", "涨跌幅": "pct_change", "成交量": "volume",
            "成交额": "amount",
        })
        df["em_code"] = df["em_code"].astype(str).str.zfill(6)
        # 东财代码 → 新浪风格代码（0/3 开头补 sh/sz 前缀）
        df["symbol"] = df["em_code"].map(
            lambda c: f"sh{c}" if c.startswith("0") else f"sz{c}"
        )
        keep = ["symbol", "em_code", "name", "price", "change", "pct_change",
                "volume", "amount"]
        df = df[[c for c in keep if c in df.columns]].copy()
        for col in ("price", "change", "pct_change", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        # 只保留配置的主要指数
        wanted = {v[1] for v in self.MAJOR_INDICES.values()}
        df = df[df["em_code"].isin(wanted)]
        return df.reset_index(drop=True)

    def get_index_daily(self, symbol: str, timeout: float | None = None, retries: int | None = None) -> pd.DataFrame:
        """指数日线历史（新浪代码格式，如 sh000001）。主源东财，失败降级新浪。"""
        try:
            df = self._call("stock_zh_index_daily_em", symbol=symbol, timeout=timeout, retries=retries)
        except DataSourceError:
            logger.warning("东方财富指数日线不可用，降级为新浪K线 symbol=%s", symbol)
            return sina_kline(symbol)
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["date", "open", "close", "high", "low", "volume", "amount"]
            )
        df = df.rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
            "最低": "low", "成交量": "volume", "成交额": "amount",
        })
        keep = ["date", "open", "close", "high", "low", "volume", "amount"]
        df = df[[c for c in keep if c in df.columns]].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in ("open", "close", "high", "low", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return df


# 全局数据源单例（进程内共享）
_data_source: AkshareDataSource | None = None


def get_data_source() -> AkshareDataSource:
    """获取数据源单例（延迟初始化）。"""
    global _data_source
    if _data_source is None:
        _data_source = AkshareDataSource()
    return _data_source
