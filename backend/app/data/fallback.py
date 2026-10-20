"""新浪/腾讯直连备用数据源。

当 akshare 主接口（东方财富 push2his 等主机）在当前网络不可用时启用
（部分网络环境会屏蔽东方财富历史行情主机，已实测遇到）。
接口均为公开免费、无需 token；返回与主源统一的 DataFrame schema。

已验证可用接口：
- 腾讯K线  : web.ifzq.gtimg.cn/appstock/app/fqkline/get
- 新浪K线  : money.finance.sina.com.cn/.../CN_MarketData.getKLineData
- 新浪实时  : hq.sinajs.cn/list=...（支持批量，GBK 编码）
"""
from __future__ import annotations

import logging
import re
import threading
import time
from urllib.parse import urlparse

import pandas as pd
import requests

logger = logging.getLogger("sas.data.fallback")

#: 浏览器请求头（部分接口对 UA/Referer 有基本校验）
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.sina.com.cn",
}

#: 新浪实时接口单批数量（实测 100-400 稳定，取保守值）
_SINA_BATCH_SIZE = 200

#: 域名级最小请求间隔（秒）。公开行情接口对突发高频请求会返回
#: 风控挑战页（腾讯实测 501 + JS 挑战），节流可显著降低触发概率。
_MIN_REQUEST_INTERVAL = 0.15
_last_request_at: dict[str, float] = {}


def _throttle(url: str) -> None:
    """按域名节流：同一域名两次请求间隔不小于 _MIN_REQUEST_INTERVAL。"""
    domain = urlparse(url).hostname or "default"
    with _throttle_lock:
        now = time.monotonic()
        wait = _MIN_REQUEST_INTERVAL - (now - _last_request_at.get(domain, 0.0))
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request_at[domain] = now


_throttle_lock = threading.Lock()


def code_to_symbol(code: str, market: str | None = None) -> str:
    """6位代码 → 新浪/腾讯风格代码（sh600519 / sz000001 / bj430047）。"""
    if market is None:
        market = "sh" if code.startswith(("60", "68", "9")) else (
            "bj" if code.startswith(("4", "8", "92")) else "sz"
        )
    return f"{market}{code}"


def _get_json(url: str, params: dict | None = None, timeout: float = 15.0) -> dict:
    _throttle(url)
    r = requests.get(url, params=params, headers=_BROWSER_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get_text(url: str, params: dict | None = None, timeout: float = 15.0) -> str:
    _throttle(url)
    r = requests.get(url, params=params, headers=_BROWSER_HEADERS, timeout=timeout)
    r.raise_for_status()
    r.encoding = "gbk"  # 新浪接口为 GBK 编码
    return r.text


# ----------------------------------------------------------------------
# K线（腾讯 / 新浪）
# ----------------------------------------------------------------------
def tencent_kline(code: str, adjust: str = "qfq", count: int = 300) -> pd.DataFrame:
    """腾讯日K线。

    URL: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
         ?param=sh600519,day,,,count,qfq
    返回 data[symbol][qfqday|hfqday|day]，每行:
        [日期, 开盘, 收盘, 最高, 最低, 成交量(手)]

    count 默认 300：策略最长需要约 70 根K线 + 指标预热，
    300 根足够且响应体小（800 根会明显拖慢全市场扫描）。

    注意：腾讯接口不返回成交额，这里按
        成交额 ≈ 成交量(手) × 100股 × (最高+最低+收盘)/3
    估算，仅用于指标计算（如"成交额过亿"筛选），与真实值有偏差。
    """
    symbol = code_to_symbol(code)
    data = _get_json(
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        {"param": f"{symbol},day,,,{count},{adjust or 'day'}"},
    )
    node = data.get("data", {}).get(symbol, {})
    key = {"qfq": "qfqday", "hfq": "hfqday"}.get(adjust, "day")
    rows = node.get(key) or node.get("day") or []
    records = []
    for row in rows:
        date, o, c, h, l, v = row[0], row[1], row[2], row[3], row[4], row[5]
        o, c, h, l, v = map(float, (o, c, h, l, v))
        amount = v * 100 * (h + l + c) / 3  # 估算成交额（元）
        records.append({
            "date": date, "open": o, "close": c, "high": h, "low": l,
            "volume": v, "amount": round(amount, 2),
        })
    df = pd.DataFrame(records)
    return _finalize_kline(df)


def sina_kline(symbol: str, datalen: int = 300) -> pd.DataFrame:
    """新浪日K线（个股与指数均支持，symbol 如 sh600519 / sh000001）。

    接口: money.finance.sina.com.cn/.../CN_MarketData.getKLineData
    返回 JSON 数组: [{"day","open","high","low","close","volume(股)"}, ...]

    该接口历史悠久、风控宽松，是腾讯被限流后的最终兜底。
    volume 单位为「股」，统一换算为「手」（÷100）与主源口径一致；
    成交额按 成交量(手)×100股×均价 估算。
    datalen 最大 1023（约4年历史）。
    """
    data = _get_json(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "CN_MarketData.getKLineData",
        {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(datalen)},
    )
    records = []
    for item in data:
        o = float(item["open"]); c = float(item["close"])
        h = float(item["high"]); l = float(item["low"])
        v = float(item["volume"]) / 100  # 股 → 手
        records.append({
            "date": item["day"], "open": o, "close": c, "high": h, "low": l,
            "volume": v,
            "amount": round(v * 100 * (h + l + c) / 3, 2),  # 估算成交额（元）
        })
    return _finalize_kline(pd.DataFrame(records))


def _finalize_kline(df: pd.DataFrame) -> pd.DataFrame:
    """统一K线字段：补 amount 空列、排序。"""
    if df.empty:
        return pd.DataFrame(
            columns=["date", "open", "close", "high", "low", "volume", "amount"]
        )
    if "amount" not in df.columns:
        df["amount"] = None
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ----------------------------------------------------------------------
# 实时快照（新浪批量）
# ----------------------------------------------------------------------
#: 解析 "var hq_str_sh600519=\"...\";" 行
_SINA_LINE_RE = re.compile(r'var hq_str_(\w+)="([^"]*)";')


def sina_quotes(symbols: list[str]) -> pd.DataFrame:
    """新浪批量实时行情。

    接口: https://hq.sinajs.cn/list=sh600519,sh000001,...
    每行字段（逗号分隔）:
        0名称 1今开 2昨收 3最新价 4最高 5最低 6买一 7卖一
        8成交量 9成交额 ... 30日期 31时间

    个股成交量单位为「股」，这里统一换算为「手」（÷100），
    与东方财富快照口径一致；成交额单位为元。
    """
    records: list[dict] = []
    for i in range(0, len(symbols), _SINA_BATCH_SIZE):
        batch = symbols[i : i + _SINA_BATCH_SIZE]
        try:
            text = _get_text("https://hq.sinajs.cn/list=" + ",".join(batch))
        except Exception as exc:  # noqa: BLE001 - 单批失败不拖垮整体
            logger.warning("新浪实时批次 %d 请求失败: %s", i // _SINA_BATCH_SIZE, exc)
            continue
        for match in _SINA_LINE_RE.finditer(text):
            symbol, payload = match.group(1), match.group(2)
            fields = payload.split(",")
            if len(fields) < 10 or not fields[3]:
                continue  # 停牌或无效代码
            try:
                name = fields[0]
                open_ = float(fields[1])
                prev_close = float(fields[2])
                price = float(fields[3])
                high = float(fields[4])
                low = float(fields[5])
                volume = float(fields[8]) / 100  # 股 → 手
                amount = float(fields[9])
            except (ValueError, IndexError):
                continue
            pct_change = (
                round((price - prev_close) / prev_close * 100, 2)
                if prev_close else None
            )
            records.append({
                "code": symbol[2:], "name": name, "price": price,
                "pct_change": pct_change, "change": round(price - prev_close, 2),
                "open": open_, "high": high, "low": low, "prev_close": prev_close,
                "volume": volume, "amount": amount,
            })
    return pd.DataFrame(records)
