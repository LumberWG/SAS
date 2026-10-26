"""新浪/腾讯/雪球直连备用数据源。

当 akshare 主接口（东方财富 push2his 等主机）在当前网络不可用时启用
（部分网络环境会屏蔽东方财富历史行情主机，已实测遇到）。
接口均为公开免费（雪球需 cookie 握手，无需账号）；返回与主源统一的 DataFrame schema。

已验证可用接口：
- 雪球K线  : stock.xueqiu.com/v5/stock/chart/kline.json（需先访问 /hq 取 token，带真实成交额）
- 腾讯K线  : proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get
             web.ifzq.gtimg.cn 同源接口在部分网络返回 501，已改走 proxy.finance
- 腾讯K线备: ifzq.gtimg.cn/appstock/app/kline/kline（不复权）
- 新浪K线  : money.finance.sina.com.cn/.../CN_MarketData.getKLineData
- 新浪实时 : hq.sinajs.cn/list=...（支持批量，GBK 编码）
- 东财列表 : push2.eastmoney.com/api/qt/clist/get（全市场代码表）
- 新浪列表 : vip.stock.finance.sina.com.cn/.../Market_Center.getHQNodeData（分页全量）
- 上交所列表: query.sse.com.cn/security/stock/getStockListData2.do（主板+科创板，GBK）
"""
from __future__ import annotations

import json
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
    """按域名节流：同一域名两次请求间隔不小于 _MIN_REQUEST_INTERVAL。

    锁必须按域名独立：早期用一把全局锁 + 锁内 sleep，会让雪球/腾讯/新浪
    的请求互相排队，20 并发扫描退化成串行（实测 25 只耗时 33 秒）。
    """
    domain = urlparse(url).hostname or "default"
    with _throttle_lock:
        lock = _domain_locks.get(domain)
        if lock is None:
            lock = _domain_locks[domain] = threading.Lock()
    with lock:
        now = time.monotonic()
        wait = _MIN_REQUEST_INTERVAL - (now - _last_request_at.get(domain, 0.0))
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_request_at[domain] = now


_throttle_lock = threading.Lock()          # 仅保护 _domain_locks 字典
_domain_locks: dict[str, threading.Lock] = {}  # 每个域名一把锁

# ----------------------------------------------------------------------
# 共享连接池：行情接口每次新建 TLS 连接开销极大
# （实测新浪首连约 7-8 秒、复用连接 0.05 秒），必须复用连接，
# 否则单只股票取K线就是秒级等待，全市场扫描会退化到不可用。
# ----------------------------------------------------------------------
_session: requests.Session | None = None
_session_lock = threading.Lock()
_POOL_SIZE = 32


def _get_session() -> requests.Session:
    """获取共享 Session（连接池复用，线程安全地惰性初始化）。"""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                session = requests.Session()
                session.headers.update(_BROWSER_HEADERS)
                # 国内免费行情源一律直连：系统代理常不可用（实测会抛
                # ProxyError 导致数据源整条链路失效），与 DATA_DIRECT_ONLY 语义一致
                session.trust_env = False
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=_POOL_SIZE,
                    pool_maxsize=_POOL_SIZE,
                    max_retries=0,
                )
                session.mount("https://", adapter)
                session.mount("http://", adapter)
                _session = session
    return _session


def code_to_symbol(code: str, market: str | None = None) -> str:
    """6位代码 → 新浪/腾讯风格代码（sh600519 / sz000001 / bj920000）。

    注意 92 开头是北交所（920xxx），不能因为以 9 开头就判成上交所；
    900xxx 才是沪B。
    """
    if market is None:
        if code.startswith("92") or code.startswith(("4", "8")):
            market = "bj"
        elif code.startswith(("60", "68", "90")):
            market = "sh"
        else:
            market = "sz"
    return f"{market}{code}"


def _get_json(url: str, params: dict | None = None, timeout: float = 15.0):
    _throttle(url)
    r = _get_session().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _get_text(url: str, params: dict | None = None, timeout: float = 15.0) -> str:
    _throttle(url)
    r = _get_session().get(url, params=params, timeout=timeout)
    r.raise_for_status()
    r.encoding = "gbk"  # 新浪接口为 GBK 编码
    return r.text


# ----------------------------------------------------------------------
# K线（腾讯 / 新浪）
# ----------------------------------------------------------------------
#: 腾讯日K线可用主机（按优先级）。
#: web.ifzq.gtimg.cn 的 fqkline 接口在部分网络/风控下返回 501，
#: 而 proxy.finance.qq.com 与 ifzq.gtimg.cn 的同源接口仍可用，
#: 因此这里按顺序尝试，任一成功即返回。
_TENCENT_KLINE_ENDPOINTS = (
    ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get", True),
    ("https://ifzq.gtimg.cn/appstock/app/kline/kline", False),
)


def tencent_kline(code: str, adjust: str = "qfq", count: int = 300) -> pd.DataFrame:
    """腾讯日K线（多主机降级）。

    返回 data[symbol][qfqday|hfqday|day]，每行:
        [日期, 开盘, 收盘, 最高, 最低, 成交量(手)]

    count 默认 300：策略最长需要约 70 根K线 + 指标预热，
    300 根足够且响应体小（800 根会明显拖慢全市场扫描）。

    注意：腾讯接口不返回成交额，这里按
        成交额 ≈ 成交量(手) × 100股 × (最高+最低+收盘)/3
    估算，仅用于指标计算（如"成交额过亿"筛选），与真实值有偏差。
    """
    symbol = code_to_symbol(code)
    key = {"qfq": "qfqday", "hfq": "hfqday"}.get(adjust, "day")
    rows: list = []
    errors: list[Exception] = []
    for url, supports_fq in _TENCENT_KLINE_ENDPOINTS:
        param = (
            f"{symbol},day,,,{count},{adjust or 'day'}"
            if supports_fq
            else f"{symbol},day,,,{count}"
        )
        try:
            data = _get_json(url, {"param": param})
        except Exception as exc:  # noqa: BLE001 - 换下一个主机
            errors.append(exc)
            continue
        node = (data.get("data") or {}).get(symbol) or {}
        rows = node.get(key) or node.get("day") or []
        if rows:
            break
    if not rows and errors:
        raise errors[0]

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
    # 无效代码（如市场前缀错误的北交所代码）新浪返回 null，需按空数据处理
    if not data or not isinstance(data, list):
        return _finalize_kline(pd.DataFrame())
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


# ----------------------------------------------------------------------
# 分时（腾讯逐分钟）
# ----------------------------------------------------------------------
#: 腾讯分时可用主机（与日K同源，web 主机在部分网络返回 501）
_TENCENT_MINUTE_ENDPOINTS = (
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/minute/query",
    "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
)
_INTRADAY_COLUMNS = ["date", "time", "price", "avg_price", "volume", "amount"]


def _date_str(raw: str) -> str:
    """YYYYMMDD → YYYY-MM-DD（无法识别时原样返回）。"""
    raw = str(raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def tencent_minute(code: str) -> pd.DataFrame:
    """腾讯分时数据（当日逐分钟价格 + 累计量额）。

    接口: .../appstock/app/minute/query?code=sh600519
    返回 ``data[symbol].data.data`` 为字符串数组，每行:
        "0930 1325.40 905 119948700.00"
    即 时间 价格 **累计成交量(手)** **累计成交额(元)**。
    这里换算为「分钟成交量」与「当日累计均价」，与东财逐笔聚合口径一致。

    收盘后仍返回最近一个交易日的数据，因此非交易时段分时图也能展示。
    """
    symbol = code_to_symbol(code)
    rows: list = []
    date = ""
    errors: list[Exception] = []
    for url in _TENCENT_MINUTE_ENDPOINTS:
        try:
            data = _get_json(url, {"code": symbol}, timeout=8.0)
        except Exception as exc:  # noqa: BLE001 - 换下一个主机
            errors.append(exc)
            continue
        payload = ((data.get("data") or {}).get(symbol) or {}).get("data") or {}
        rows = payload.get("data") or []
        date = _date_str(payload.get("date") or "")
        if rows:
            break
    if not rows:
        if errors:
            raise errors[0]
        return pd.DataFrame(columns=_INTRADAY_COLUMNS)

    records: list[dict] = []
    prev_volume = 0.0    # 上一分钟的累计成交量
    prev_amount = 0.0    # 上一分钟的累计成交额
    for item in rows:
        parts = str(item).split()
        if len(parts) < 3:
            continue
        try:
            price = float(parts[1])
            cum_volume = float(parts[2])
            cum_amount = float(parts[3]) if len(parts) > 3 else None
        except ValueError:
            continue
        volume = max(cum_volume - prev_volume, 0.0)
        prev_volume = cum_volume
        if cum_amount is not None:
            amount = max(cum_amount - prev_amount, 0.0)
            total_amount = cum_amount
        else:
            # 源缺成交额时按成交价估算（只影响均价曲线，不影响价格）
            amount = price * volume * 100
            total_amount = prev_amount + amount
        prev_amount = total_amount
        hhmm = parts[0]
        records.append({
            "date": date,
            "time": f"{hhmm[:2]}:{hhmm[2:4]}",
            "price": price,
            "avg_price": (
                round(total_amount / (cum_volume * 100), 3) if cum_volume > 0 else None
            ),
            "volume": round(volume, 2),
            "amount": round(amount, 2),
        })
    return pd.DataFrame(records, columns=_INTRADAY_COLUMNS)


# ----------------------------------------------------------------------
# 雪球K线（需 cookie 握手，返回真实成交额）
# ----------------------------------------------------------------------
_XUEQIU_HOME = "https://xueqiu.com/hq"
_XUEQIU_KLINE_URL = "https://stock.xueqiu.com/v5/stock/chart/kline.json"
#: token 有效期（握手一次可复用；接口返回 4xx 时会自动重新握手）
_XUEQIU_TOKEN_TTL = 1800.0
_xq_session: requests.Session | None = None
_xq_session_at: float = 0.0
_xq_lock = threading.Lock()


def _xueqiu_session(force: bool = False) -> requests.Session:
    """获取带 token 的雪球 Session（惰性握手，过期或失效后重握）。"""
    global _xq_session, _xq_session_at
    now = time.monotonic()
    if not force and _xq_session is not None and now - _xq_session_at < _XUEQIU_TOKEN_TTL:
        return _xq_session
    with _xq_lock:
        if (
            not force
            and _xq_session is not None
            and time.monotonic() - _xq_session_at < _XUEQIU_TOKEN_TTL
        ):
            return _xq_session
        session = requests.Session()
        session.trust_env = False  # 与共享 Session 一致：国内源直连
        session.headers.update(
            {"User-Agent": _BROWSER_HEADERS["User-Agent"],
             "Referer": "https://xueqiu.com/hq"}
        )
        # 扫描并发最高 scan_concurrency（默认 20），默认 10 连接池会丢弃连接
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE, max_retries=0
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        # 首页只下发 acw_tc（风控 cookie），必须访问 /hq 才会下发 xq_a_token
        session.get(_XUEQIU_HOME, timeout=10.0)
        _xq_session = session
        _xq_session_at = time.monotonic()
    return _xq_session


def xueqiu_kline(code: str, adjust: str = "qfq", count: int = 300) -> pd.DataFrame:
    """雪球日K线（含**真实成交额**，腾讯/新浪只有估算值）。

    接口: stock.xueqiu.com/v5/stock/chart/kline.json
         ?symbol=SH600519&begin=<ms>&period=day&type=before&count=-300
    返回 data.column 列名 + data.item 行数组，按列名取用（避免列序变化踩坑）。
    token 失效时自动重新握手并重试一次。
    """
    symbol = code_to_symbol(code).upper()  # 雪球用大写 sh600519 → SH600519
    period_type = {"qfq": "before", "hfq": "after"}.get(adjust, "normal")
    resp: requests.Response | None = None
    last_exc: Exception | None = None
    for attempt in (1, 2):
        session = _xueqiu_session(force=attempt > 1)
        try:
            r = session.get(
                _XUEQIU_KLINE_URL,
                params={
                    "symbol": symbol,
                    "begin": int(time.time() * 1000),
                    "period": "day",
                    "type": period_type,
                    "count": f"-{count}",
                    "indicator": "kline",
                },
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常可直接重试
            last_exc = exc
            continue
        if r.status_code == 200:
            resp = r
            break
        last_exc = RuntimeError(f"雪球K线 {r.status_code}: {r.text[:80]}")
        if r.status_code not in (400, 401, 403):
            break  # 非鉴权问题（如风控 5xx）无需重握
    if resp is None:
        raise last_exc or RuntimeError("雪球K线不可用")

    payload = resp.json().get("data") or {}
    cols = payload.get("column") or []
    rows = payload.get("item") or []
    if not rows or not cols:
        return _finalize_kline(pd.DataFrame())
    raw = pd.DataFrame(rows, columns=cols)
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["timestamp"], unit="ms", utc=True)
            .dt.tz_convert("Asia/Shanghai")
            .dt.strftime("%Y-%m-%d"),
            "open": pd.to_numeric(raw["open"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "high": pd.to_numeric(raw["high"], errors="coerce"),
            "low": pd.to_numeric(raw["low"], errors="coerce"),
            # 雪球成交量单位为「股」，统一换算为「手」
            "volume": pd.to_numeric(raw["volume"], errors="coerce") / 100.0,
            # 真实成交额（元），无需估算
            "amount": pd.to_numeric(
                raw.get("amount", pd.Series([None] * len(raw))), errors="coerce"
            ),
        }
    )
    df = df.dropna(subset=["date", "open", "close"])
    return _finalize_kline(df)


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
# 全A股列表（东方财富 push2，单接口分页取全市场）
# ----------------------------------------------------------------------
#: push2 可以访问（K线走的 push2his 在部分网络被墙，列表接口不受影响）
_EM_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
#: 沪深京 A 股：沪主板/科创板 + 深主板/创业板 + 北交所
_EM_MARKET_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def em_stock_list(page_size: int = 1000) -> pd.DataFrame:
    """东方财富 push2 全A股代码/名称（分页拉全量）。

    相比 akshare 的 stock_info_a_code_name（内部多次分段请求，部分网络
    会超时数分钟），该接口一次请求即可返回上千条，配合连接池几十毫秒。
    返回列: code / name（market 由上层补齐）。
    """
    records: list[dict] = []
    page = 1
    total = 0
    while True:
        data = _get_json(
            _EM_LIST_URL,
            {
                "pn": str(page), "pz": str(page_size), "po": "0", "np": "1",
                "fltt": "2", "invt": "2", "fid": "f12", "fs": _EM_MARKET_FILTER,
                "fields": "f12,f14",
            },
            timeout=10.0,
        )
        payload = data.get("data") or {}
        diff = payload.get("diff") or []
        if not diff:
            break
        if isinstance(diff, dict):  # 个别版本返回 {"0": {...}, ...}
            diff = list(diff.values())
        for row in diff:
            code = str(row.get("f12") or "").strip().zfill(6)
            name = str(row.get("f14") or "").strip()
            if code and code.isdigit():
                records.append({"code": code, "name": name})
        total = int(payload.get("total") or 0)
        if len(diff) < page_size or (total and len(records) >= total):
            break
        page += 1
        if page > 20:  # 防御：异常分页时避免死循环
            break
    if not records:
        return pd.DataFrame(columns=["code", "name"])
    return (
        pd.DataFrame(records)
        .drop_duplicates("code")
        .reset_index(drop=True)
    )


_SINA_LIST_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)
_SINA_COUNT_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeStockCount"
)
_SINA_PAGE_SIZE = 100
_SINA_MAX_PAGES = 80


def _sina_list_page(page: int) -> list[dict]:
    """取新浪列表单页；失败抛异常由调用方处理。"""
    return _get_json(
        _SINA_LIST_URL,
        {
            "page": str(page), "num": str(_SINA_PAGE_SIZE),
            "sort": "symbol", "asc": "1", "node": "hs_a",
        },
        timeout=10.0,
    )


def sina_stock_list() -> pd.DataFrame:
    """新浪全A股代码/名称（分页取全量，含沪深京）。

    该接口与实时报价同源、风控宽松，作为股票池的可靠全量来源。
    串行翻 56 页约需 30 秒（每页 0.3s + 域名节流），会顶到前端 30s
    超时线，因此改为并行翻页（域名节流仍保证同域请求间隔）。
    返回列: code / name（market 由上层补齐）。
    """
    from concurrent.futures import ThreadPoolExecutor

    try:
        total = int(_get_json(_SINA_COUNT_URL, {"node": "hs_a"}, timeout=8.0))
    except Exception:  # noqa: BLE001 - 拿不到总数时按空页终止
        total = 0

    pages = min(
        _SINA_MAX_PAGES,
        max(1, -(-total // _SINA_PAGE_SIZE)) if total else _SINA_MAX_PAGES,
    )

    def fetch(page: int) -> list[dict]:
        try:
            return _sina_list_page(page) or []
        except Exception as exc:  # noqa: BLE001 - 单页失败不影响其他页
            logger.warning("新浪列表第 %d 页失败: %s", page, exc)
            return []

    page_rows: list[list[dict]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        page_rows = list(pool.map(fetch, range(1, pages + 1)))

    records: list[dict] = []
    for rows in page_rows:
        if not rows:
            continue
        for row in rows:
            code = str(row.get("code") or "").strip().zfill(6)
            if code.isdigit():
                records.append(
                    {"code": code, "name": str(row.get("name") or "").strip()}
                )
        if total and len(records) >= total:
            break

    if not records:
        return pd.DataFrame(columns=["code", "name"])
    return pd.DataFrame(records).drop_duplicates("code").reset_index(drop=True)


# ----------------------------------------------------------------------
# 上交所官方 A 股列表（沪市独立校验源）
# ----------------------------------------------------------------------
_SSE_LIST_URL = "http://query.sse.com.cn/security/stock/getStockListData2.do"
#: stockType: 1=主板 A 股，8=科创板
_SSE_STOCK_TYPES = ("1", "8")


def sse_stock_list() -> pd.DataFrame:
    """上交所官方 A 股代码/名称（主板 + 科创板）。

    交易所官方数据，与行情站列表互相校验：用于补齐/核对沪市覆盖。
    返回体为 GBK 编码 JSON（Content-Type 声明 UTF-8，需按 GBK 解码）。
    返回列: code / name（market 由上层补齐）。
    """
    records: list[dict] = []
    errors: list[Exception] = []
    for stock_type in _SSE_STOCK_TYPES:
        try:
            r = _get_session().get(
                _SSE_LIST_URL,
                params={
                    "isPagination": "true", "stockCode": "", "csrcCode": "",
                    "areaName": "", "stockType": stock_type,
                    "pageHelp.cacheSize": "1", "pageHelp.beginPage": "1",
                    "pageHelp.collapsepageSize": "1", "pageHelp.pageSize": "5000",
                    "pageHelp.pageNo": "1",
                },
                headers={"Referer": "http://www.sse.com.cn/"},
                timeout=20.0,
            )
            r.raise_for_status()
            payload = json.loads(r.content.decode("gbk", "replace"))
        except Exception as exc:  # noqa: BLE001 - 单个板块失败不影响另一个
            errors.append(exc)
            continue
        rows = (payload.get("pageHelp") or {}).get("data") or []
        for row in rows:
            code = str(row.get("SECURITY_CODE_A") or "").strip()
            if len(code) == 6 and code.isdigit():
                records.append(
                    {"code": code, "name": str(row.get("COMPANY_ABBR") or "").strip()}
                )

    if not records:
        if errors:
            raise errors[0]
        return pd.DataFrame(columns=["code", "name"])
    return pd.DataFrame(records).drop_duplicates("code").reset_index(drop=True)


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
        batch_no = i // _SINA_BATCH_SIZE
        text = None
        # 单批重试一次：新浪偶发 456/限流，缺一批就少 200 只股票，
        # 会让快照大面积缩水（进而影响「成交额前 N」筛选）
        for attempt in (1, 2):
            try:
                text = _get_text("https://hq.sinajs.cn/list=" + ",".join(batch))
                break
            except Exception as exc:  # noqa: BLE001 - 单批失败不拖垮整体
                if attempt == 2:
                    logger.warning("新浪实时批次 %d 请求失败: %s", batch_no, exc)
                else:
                    time.sleep(0.3)
        if text is None:
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
            # 未开盘/集合竞价前/停牌时新浪返回「最新价 = 0」，
            # 此时用 0/昨收 会算出 -100%（把全市场误判成跌停），
            # 因此无有效成交价时涨跌幅与涨跌额一律置 None。
            if price <= 0:
                pct_change = None
                change = None
            else:
                pct_change = (
                    round((price - prev_close) / prev_close * 100, 2)
                    if prev_close else None
                )
                change = round(price - prev_close, 2)
            records.append({
                "code": symbol[2:], "name": name, "price": price,
                "pct_change": pct_change, "change": change,
                "open": open_, "high": high, "low": low, "prev_close": prev_close,
                "volume": volume, "amount": amount,
            })
    return pd.DataFrame(records)
