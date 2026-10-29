# -*- coding: utf-8 -*-
"""离线批量预下载历史分钟线到本地，供回测 intraday 使用（独立运行工具）。

用法：
    # 先小批量验证数据源可用
    python scripts/preload_minutes.py --limit 20
    # 单进程全量（建议挂 Windows 任务计划程序，夜间跑；断了重跑会自动跳过已缓存的）
    python scripts/preload_minutes.py
    # 并行分片（推荐，快 2~4 倍）：把全市场拆成 N 个进程各跑 1/N，互不干扰
    # 见 preload_minutes_parallel.py —— 它会自动 spawn N 个子进程并把输出写到 logs/minute5_shard_*.log
    python scripts/preload_minutes_parallel.py --shards 4
    # 也可手动跑单个分片（用于排查 / 单独续传某一片）：
    python scripts/preload_minutes.py --shard 2 --shards 4 --start 2025-01-01

数据源：Baostock（免费、无需 token）。Baostock 走自有 TCP 协议、不读系统 HTTP 代理，
因此用 PySocks 把它的 socket 补丁成走 Forest 的 SOCKS5 端口(7891)，由 Forest 再按
规则把 baostock.com 路由到代理节点出网。

特点：
    - 断点续传：已缓存且覆盖区间的 code 自动跳过
    - 节流：每段请求间隔 --sleep 秒，避开数据源限频
    - 失败指数退避 + 连续失败熔断：避免限频封禁时傻等
    - 进度日志 + 跳过/成功/失败统计
输出：<data>/minute5/{code}.parquet，列 date(YYYY-MM-DD) / close(float)
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pandas as pd

# pandas 2.0+ 移除了 DataFrame.append，而 baostock 0.9.4 仍在使用
# （baostock/data/resultset.py 里 df.append(temp_df, ignore_index=True)）。
# 这里补一个兼容方法，避免 baostock.get_data() 在 pandas 3.x 下崩溃。
# 仅作用于本进程，不改动 venv 里的 pandas（以免连累 akshare）。
if not hasattr(pd.DataFrame, "append"):
    def _df_append(self, other, ignore_index=False, verify_integrity=False, sort=False):
        if other is None:
            return self if ignore_index else self.copy()
        frames = [self, other] if isinstance(other, pd.DataFrame) else [self] + list(other)
        return pd.concat(frames, ignore_index=ignore_index)
    pd.DataFrame.append = _df_append

try:
    import baostock as bs
except Exception:  # noqa: BLE001
    print("ERROR: baostock 未安装（pip install baostock）")
    sys.exit(2)

# ---- 路径解析：优先用项目配置，失败回退到相对路径 ----
_BACKEND = Path(__file__).resolve().parent.parent  # backend/
sys.path.insert(0, str(_BACKEND))
try:
    from app.config import get_settings
    KLINE_DIR = get_settings().kline_dir
except Exception:  # noqa: BLE001
    KLINE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "klines"
OUT_DIR = KLINE_DIR.parent / "minute5"

# ---- 可调参数 ----
SEG_DAYS = 240         # 每段天数。区间 665 天 -> 3 段（每段约 8300 行，Baostock 单次上限 10000）。
                       # 原来是 90 天 = 8 段，往返握手开销占到大头；240 天把请求次数砍到 3 次，
                       # 单只耗时直接降一半以上。改大前务必确认 seg*34.7 < 10000。
SLEEP = 1.0            # 请求间隔（秒），避开限频
MAX_BACKOFF = 30.0
DISABLE_AFTER = 5      # 连续失败达到此数则熔断剩余任务（Baostock 偶发限频时不至于误伤整批）
DISABLE_TTL = 180.0    # 熔断冷却（秒）：冷却后自动重连恢复，不退出进程
MAX_NET_ERR = 6        # 单只连续网络错误上限：超过则放弃该只（记失败），避免一个坏连接拖死一片
SOCKS_HOST = "127.0.0.1"
SOCKS_PORT = 7891      # Forest 的 SOCKS 端口
SOCK_TIMEOUT = 75.0   # socket 读写超时（秒），已真正注入到 PySocks（见 setup_socks_proxy）。
                       # 必须有：Baostock 的 send_msg 是 `while True: recv()`，
                       # 连接被断开后 recv 返回空字节 → 死循环空转 100% CPU、永不返回。
HARD_TIMEOUT = 120.0   # 单次请求的「硬」超时（秒）：起独立线程跑请求，超时就强关 socket 解阻塞。
                       # 这是第二道保险 —— socket 层超时偶尔仍会因为 EOF 空转而失效。
DIRECT_TIMEOUT = 30.0  # 直连时的 socket 超时：直连链路短，30s 还没回就该换段重试了
START_DEFAULT = "2025-01-01"  # 默认起始日期：只抓 2025 年以后的数据（可用 --start 覆盖）


# 保存原始 stdout：log() 永远直写它的字节流，绝不受下面 quiet_baostock() 的替换影响
# （否则 print 兜底 + _QuietStream 会互相递归）。
_RAW_OUT = sys.stdout

# baostock 内部 print 的中文错误去重表：{消息: (最后一次输出时间, 期间被压掉的次数)}
_bs_seen: "dict[str, tuple[float, int]]" = {}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
    # 始终以 UTF-8 字节写出，彻底避免 Windows GBK 控制台/管道编码导致的崩溃
    try:
        _RAW_OUT.buffer.write(line.encode("utf-8", "replace"))
        _RAW_OUT.buffer.flush()
    except Exception:  # noqa: BLE001
        try:
            print(line, flush=True, end="")
        except Exception:  # noqa: BLE001
            pass


def _emit_quiet(prefix: str, s: str) -> None:
    """把第三方库的一条输出转成 log()，同一条 60 秒内只打一次（刷屏治理）。"""
    key = s[:200]
    now = time.time()
    last, suppressed = _bs_seen.get(key, (0.0, 0))
    if last and (now - last) < 60.0:
        _bs_seen[key] = (last, suppressed + 1)
        return
    extra = f"（近 60s 内另重复 {suppressed} 次）" if suppressed else ""
    _bs_seen[key] = (now, 0)
    log(f"{prefix} {s}{extra}")


class _QuietStream:
    """替换 sys.stdout / sys.stderr，让第三方库（baostock）的 print 不再乱码、不再刷屏。

    为什么需要：baostock 0.9.4 在 socket 出错时直接 print() 打中文，例如
        [WinError 10053] 您的主机中的软件中止了一个已建立的连接
    它走 sys.stdout 的文本层，Windows 下按 GBK 编码写进管道；而父进程
    (preload_minutes_parallel.py) 按 UTF-8 解码 -> 整行变成一串方块乱码。
    同时它每个分段都打一遍，几分钟就能刷出上万行。

    .buffer 透传给原始 stream，保证 log() 的字节直写照常工作。
    """

    def __init__(self, orig, prefix: str) -> None:
        self._orig = orig
        self._prefix = prefix
        self.buffer = getattr(orig, "buffer", orig)
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, s):  # noqa: D401
        try:
            txt = s.strip()
            if txt:
                _emit_quiet(self._prefix, txt)
        except Exception:  # noqa: BLE001
            pass
        return len(s) if isinstance(s, str) else 0

    def writelines(self, lines):
        for ln in lines:
            self.write(ln)

    def flush(self):
        try:
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self):
        return False

    def fileno(self):
        return self._orig.fileno()

    def reconfigure(self, **kw):  # noqa: D401 - 兼容有代码调用 reconfigure 的场景
        return None


def quiet_baostock() -> None:
    """接管 stdout/stderr：中文错误统一按 UTF-8 经 log() 输出，并做去重限流。"""
    if not isinstance(sys.stdout, _QuietStream):
        sys.stdout = _QuietStream(sys.stdout, "[bs]")
    if not isinstance(sys.stderr, _QuietStream):
        sys.stderr = _QuietStream(sys.stderr, "[bs-err]")


# baostock 网络类错误特征（这些错要重连后重试，绝不能当成「该股票没数据」）
NET_ERR_TOKENS = (
    "10002007", "10053", "10054", "10055", "10060", "10061",
    "网络", "接收", "连接", "超时", "断开", "中止", "登录",
    "timed out", "timeout", "reset", "aborted", "refused", "broken pipe",
)


def is_net_error(code: str, msg: str) -> bool:
    s = f"{code} {msg}".lower()
    return any(t.lower() in s for t in NET_ERR_TOKENS)


_ORIG_SOCKET_CLS = socket.socket  # 补丁前的原始 socket 类（PySocks 不吃 setdefaulttimeout）


def _force_close_bs_socket() -> None:
    """强制关闭 baostock 的全局连接，用来解除阻塞中的 recv/send。

    Baostock 的 send_msg 是 `while True: recv(8192)`，代理挂住或对端半关闭时
    这个循环会永远不返回；从另一个线程 close 掉 socket，recv 立刻抛错跳出。
    """
    try:
        import baostock.common.context as _ctx
    except Exception:  # noqa: BLE001
        return
    s = getattr(_ctx, "default_socket", None)
    if s is None:
        return
    try:
        s.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001
        pass
    try:
        s.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        _ctx.default_socket = None
    except Exception:  # noqa: BLE001
        pass


def _timed_call(fn, timeout: float = HARD_TIMEOUT, label: str = "请求"):
    """在独立线程里执行 fn，超时则强关 baostock socket 并抛 TimeoutError。

    为什么需要：PySocks + baostock 的 recv 死循环组合下，socket 层超时并不能保证
    一定返回（对端半关闭时 recv 直接返回 b""，循环空转、永不超时）。这层硬超时是
    最后的保险，代价是一次请求最多浪费 timeout 秒。
    """
    box: dict = {}

    def _run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        _force_close_bs_socket()
        th.join(5.0)
        raise TimeoutError(f"{label} 硬超时 {timeout:.0f}s（已强制关闭连接）")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def setup_socks_proxy(host: str = SOCKS_HOST, port: int = SOCKS_PORT) -> bool:
    """让 Baostock 的原生 socket 走 Forest 的 SOCKS5 代理。

    Baostock 内部用 socket 直连 public-api.baostock.com:10030，不读系统 HTTP 代理、
    也不读 HTTP_PROXY 环境变量，因此必须在进程内用 PySocks 把默认 socket 替换掉。
    """
    try:
        import socks
        socks.set_default_proxy(socks.SOCKS5, host, port)

        # 【关键坑】PySocks 的 socksocket 有自己的 _timeout 属性，默认 None，
        # 且它 **不读 socket.setdefaulttimeout()**（connect 时用的是 self._timeout）。
        # 结果：socket.setdefaulttimeout(90) 形同虚设，代理一断 recv 就永久阻塞，
        # 整个分片挂死、日志停更（21:36 那次卡死就是这么来的）。
        # 唯一解法：子类化并强制把 _timeout 写成我们的超时值。
        class _TimedSocksSocket(socks.socksocket):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._timeout = SOCK_TIMEOUT
                try:
                    _ORIG_SOCKET_CLS.settimeout(self, SOCK_TIMEOUT)
                except Exception:  # noqa: BLE001
                    pass

            def settimeout(self, t):
                # PySocks 内部 setblocking(True) 会调 settimeout(None) 关掉超时，
                # 这里把 None 视作「恢复默认超时」，避免超时被悄悄清掉。
                super().settimeout(SOCK_TIMEOUT if t is None else t)

            def setblocking(self, v):
                super().settimeout(SOCK_TIMEOUT)

        socket.setdefaulttimeout(SOCK_TIMEOUT)
        socket.socket = _TimedSocksSocket
        log(f"已启用 SOCKS5 代理: {host}:{port}（Baostock 流量将经 Forest 出网，socket 超时 {SOCK_TIMEOUT:.0f}s）")
        return True
    except Exception as _e:  # noqa: BLE001
        log(f"  [警告] SOCKS5 代理补丁失败: {_e}")
        return False


def _to_baostock_code(code: str) -> "str | None":
    """000001 -> sz.000001 ; 600000 -> sh.600000 ; 8/4 开头 -> bj（Baostock 分钟线可能不支持）"""
    if not code.isdigit() or len(code) != 6:
        return None
    if code.startswith("6") or code.startswith("9"):
        return "sh." + code
    if code.startswith(("0", "3")):
        return "sz." + code
    if code.startswith(("8", "4")):
        return "bj." + code
    return None


def code_range(code: str, kdir: Path):
    p = kdir / f"{code}.parquet"
    if not p.exists():
        return None, None
    try:
        df = pd.read_parquet(p)
        if df.empty or "date" not in df:
            return None, None
        return str(df["date"].iloc[0]), str(df["date"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None, None


def already_cached(code: str, start: str, end: str, odir: Path) -> bool:
    p = odir / f"{code}.parquet"
    if not p.exists():
        return False
    try:
        df = pd.read_parquet(p)
        if df.empty or "date" not in df:
            return False
        # minute5 的 date 是完整时间戳(YYYY-MM-DD HH:MM:SS)，比较时只取日期部分
        first = str(df["date"].iloc[0])[:10]
        last = str(df["date"].iloc[-1])[:10]
        return first <= start and last >= end
    except Exception:  # noqa: BLE001
        return False


def existing_last_date(code: str, odir: Path) -> "str | None":
    """已落盘文件的最后一条日期（用于断点续传：下次从其后一天继续）。"""
    p = odir / f"{code}.parquet"
    if not p.exists():
        return None
    try:
        df = pd.read_parquet(p)
        if df.empty or "date" not in df:
            return None
        return str(df["date"].iloc[-1])
    except Exception:  # noqa: BLE001
        return None


def split_chunks(codes: list[str], n: int) -> list[list[str]]:
    """把 codes 尽量均匀切成 n 片（余数分摊到前几片），用于并行分片。"""
    n = max(1, n)
    base = len(codes) // n
    rem = len(codes) % n
    out, idx = [], 0
    for k in range(n):
        size = base + (1 if k < rem else 0)
        out.append(codes[idx: idx + size])
        idx += size
    return out


# Baostock 黑名单（10001011）：出口 IP 被反爬系统封禁。重试毫无意义，只会加深封禁，
# 必须立刻整体停机并提示换出口。直连实测 25 请求/分钟跑 14 分钟就被封。
BANNED = {"hit": False, "msg": ""}


def is_ban_error(code, msg) -> bool:
    s = f"{code} {msg}"
    return "10001011" in s or "黑名单" in s


def mark_banned(code, msg) -> None:
    if not BANNED["hit"]:
        BANNED["hit"] = True
        BANNED["msg"] = f"{code} {msg}".strip()
        log("=" * 60)
        log(f"[FATAL] Baostock 返回黑名单：{BANNED['msg']}")
        log("        本机出口 IP 已被封，继续重试只会加重封禁。")
        log("        处理办法：改用 --proxy（走 Forest 代理换出口 IP），或等封禁解除后重跑。")
        log("=" * 60)


def relogin(max_try: int = 3) -> bool:
    """会话被代理断开后重新登录（断线自愈）。返回是否成功。"""
    try:
        bs.logout()
    except Exception:  # noqa: BLE001
        pass
    for k in range(max_try):
        try:
            lg = _timed_call(bs.login, HARD_TIMEOUT, "重新登录")
            if getattr(lg, "error_code", "1") == "0":
                return True
            ec, em = getattr(lg, "error_code", "?"), getattr(lg, "error_msg", "")
            if is_ban_error(ec, em):
                mark_banned(ec, em)
                return False  # 不再重试
            log(f"  [WARN] 重新登录返回错误: {ec} {em}")
        except Exception as _e:  # noqa: BLE001
            log(f"  [WARN] 重新登录异常({k + 1}/{max_try}): {type(_e).__name__}: {str(_e)[:120]}")
        if BANNED["hit"]:
            return False
        time.sleep(2.0 * (k + 1))
    return False


def fetch_baostock(code: str, start: str, end: str, period: str, sleep: float) -> "pd.DataFrame | None":
    bcode = _to_baostock_code(code)
    if bcode is None:
        log(f"  [跳过] {code} 非沪深代码，Baostock 不支持")
        return None
    cur = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    start_org = cur
    start_t = time.time()
    budget = 1800.0  # 单只总预算（秒），超时则保留已抓数据返回（不再整只丢弃）
    seg_err = 0      # 连续「数据源拒绝」计数，避免单段坏数据死循环
    net_err = 0      # 连续「网络错误」计数，达到上限才放弃该只（下次重跑会重试）
    err_hit = False  # 是否发生过网络/异常类错误（用于区分「抓取失败」与「本来就没数据」）
    seg_idx = 0
    total_segs = max(1, int((end_ts - start_org).days) // SEG_DAYS + 1)
    parts: list[pd.DataFrame] = []
    while cur <= end_ts:
        seg_idx += 1
        if time.time() - start_t > budget:
            if parts:
                log(f"  [提示] {code} 已达单只预算({budget:.0f}s)，保留已抓 {len(parts)} 段返回")
            else:
                log(f"  [DEBUG] {code} 单只预算内无数据（可能该区间无 5 分钟线/超范围）")
            break
        nxt = min(cur + pd.Timedelta(days=SEG_DAYS), end_ts)
        s, e = cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")
        try:
            rs = _timed_call(
                lambda: bs.query_history_k_data_plus(
                    bcode,
                    "date,time,code,open,high,low,close,volume,amount",
                    start_date=s, end_date=e,
                    frequency=str(period), adjustflag="3",
                ),
                HARD_TIMEOUT, f"{code} 段 {s}~{e}",
            )
            if rs.error_code != "0":
                # 【关键】任何非零 error_code 都必须记成「出过错」= 算抓取失败。
                # 之前这里没设 err_hit，导致 10002007（网络接收错误）的股票在最后被
                # `return None if err_hit else 空表` 判成「区间内无数据 -> 跳过」，
                # 于是整片股票被静默划掉、数据永久漏抓，日志还显示一切正常。
                err_hit = True
                emsg = f"{rs.error_code} {rs.error_msg}".strip()
                if is_ban_error(rs.error_code, rs.error_msg):
                    mark_banned(rs.error_code, rs.error_msg)
                    break  # 被封了，本只直接放弃，主循环会整体停机
                if is_net_error(rs.error_code, rs.error_msg):
                    net_err += 1
                    log(f"  [WARN] {code} 段 {s}~{e} 网络错误({net_err}/{MAX_NET_ERR}) {emsg}")
                    if net_err >= MAX_NET_ERR:
                        log(f"  [WARN] {code} 网络错误已达上限({MAX_NET_ERR})，放弃该只"
                            f"（记为失败，下次重跑会重试）")
                        break
                    if net_err % 2 == 1:  # 隔次重连，避免每个错误都重登录
                        if not relogin():
                            log("  [WARN] 重新登录失败（Forest 代理可能已断），结束本只")
                            break
                    time.sleep(min(MAX_BACKOFF, 2.0 * net_err))
                    continue  # 关键：不推进 cur，重试同一段，不留数据缺口
                # 非网络类错误（代码不被支持 / 参数非法）：跳过本段继续下一段
                seg_err += 1
                log(f"  [DEBUG] {code} 段 {s}~{e} 数据源拒绝({seg_err}/3): {emsg}")
                if seg_err >= 3:
                    log(f"  [DEBUG] {code} 连续被拒 3 次，停止本只")
                    break
                time.sleep(sleep)
                cur = nxt + pd.Timedelta(days=1)
                continue
            seg_err = 0
            net_err = 0
            df = _timed_call(rs.get_data, HARD_TIMEOUT, f"{code} 段 {s}~{e} 取数")
        except Exception as _e:  # 网络/限频异常（含 socket 超时）：重连后重试本段
            err_hit = True
            seg_err += 1
            log(f"  [WARN] {code} 段 {s}~{e} 异常({seg_err}/3): {type(_e).__name__}: {str(_e)[:120]}")
            if seg_err >= 3:
                log(f"  [WARN] {code} 连续 3 段异常，放弃该只（已抓 {len(parts)} 段会保留落盘）")
                break
            if not relogin():
                log("  [WARN] 重新登录失败（Forest 代理可能已断），结束本只")
                break
            time.sleep(min(MAX_BACKOFF, 3.0 * seg_err))
            continue  # 不推进 cur：重试同一段，避免数据缺口
        if df is not None and not df.empty:
            df = df.copy()
            # Baostock 的 time 列为 17 位盘中时间戳(如 20220901093500000)，
            # 必须据此生成完整时间戳；若只用日级 date 去重会把同日的 5 分钟 bar 压成 1 行。
            dt = pd.to_datetime(df["time"], format="%Y%m%d%H%M%S%f", errors="coerce")
            df["date"] = dt.dt.strftime("%Y-%m-%d %H:%M:%S")
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            parts.append(df[["date", "close"]].dropna())
            log(f"  . {code} 段 {seg_idx}/{total_segs} {s}~{e} -> {len(df)} 行")
        else:
            log(f"  . {code} 段 {seg_idx}/{total_segs} {s}~{e} -> 空")
        time.sleep(sleep)
        cur = nxt + pd.Timedelta(days=1)
    if not parts:
        # 出过错 → None（算失败）；没出错只是没数据 → 返回空表（算跳过，不算失败）
        return None if err_hit else pd.DataFrame(columns=["date", "close"])
    out = pd.concat(parts, ignore_index=True).drop_duplicates("date").sort_values("date")
    return out


def probe_last_minute_date(period: str = "5") -> "str | None":
    """探测 Baostock 当前实际可用的最近分钟线日期（返回如 2026-10-28）。

    为什么需要：日线（kline）往往先更新到「今天」，但 Baostock 的分钟线常滞后
    1 个交易日。若不把结束日期夹住，断点续传会把起点算成「今天」，结果每只股票
    都只请求「今天~今天」→ 空数据 → 被误判为失败 → 连续失败直接熔断。
    """
    today = pd.Timestamp.today().normalize()
    start = (today - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    for code in ("sz.000001", "sh.600000"):
        try:
            rs = _timed_call(
                lambda: bs.query_history_k_data_plus(
                    code, "date,time,close",
                    start_date=start, end_date=today.strftime("%Y-%m-%d"),
                    frequency=str(period), adjustflag="3",
                ),
                HARD_TIMEOUT, f"探测 {code}",
            )
            if getattr(rs, "error_code", "1") != "0":
                continue
            df = _timed_call(rs.get_data, HARD_TIMEOUT, f"探测 {code} 取数")
            if df is not None and not df.empty:
                return str(df["date"].iloc[-1])
        except Exception:  # noqa: BLE001
            continue
    return None


def main() -> None:
    # 第一件事：接管 stdout/stderr，把 baostock 的中文错误统一成 UTF-8 并去重限流
    # （否则 WinError 10053 那类消息在 Windows 管道里按 GBK 输出 -> 父进程读成乱码方块）
    quiet_baostock()

    ap = argparse.ArgumentParser(description="离线预下载历史分钟线到本地（Baostock 数据源）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理前 N 只（0=全量）")
    ap.add_argument("--period", default="5", help="分钟周期：5 / 15 / 30 / 60")
    ap.add_argument("--kline-dir", default=str(KLINE_DIR))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--sleep", type=float, default=SLEEP, help="请求间隔秒数")
    ap.add_argument("--start", default=None, help=f"强制起始日期（默认 {START_DEFAULT}）")
    ap.add_argument("--end", default=None, help="强制结束日期（覆盖推断）")
    ap.add_argument("--shard", type=int, default=1, help="当前分片序号(1-based)，配合 --shards 使用")
    ap.add_argument("--shards", type=int, default=1, help="总分片数；1=不分片（单进程）")
    ap.add_argument("--proxy", action="store_true",
                    help="强制走 SOCKS5 代理（默认直连 Baostock；实测直连快约 2 倍）")
    args = ap.parse_args()

    kdir = Path(args.kline_dir)
    odir = Path(args.out_dir)
    odir.mkdir(parents=True, exist_ok=True)

    # 出网方式：默认直连（实测比走 Forest SOCKS5 快约 2 倍：3.79s vs 7.81s / 3072 行）。
    # Baostock 是国内的 TCP 服务，代理反而绕路 + 增加断连风险（WinError 10053）。
    # 若直连被限频/不通，加 --proxy 切回代理。
    if args.proxy:
        setup_socks_proxy()
    else:
        socket.setdefaulttimeout(DIRECT_TIMEOUT)  # 直连也要超时，防止 recv 空转死循环
        log(f"直连 Baostock（未走代理），socket 超时 {DIRECT_TIMEOUT:.0f}s；如需代理请加 --proxy")

    # 登录 Baostock（无需 token）—— 同时充当连通性探针。
    # 必须带重试：代理节点偶尔抽风 1~2 次就恢复，直接 sys.exit 会让整个分片白白退出。
    login_ok = False
    for attempt in range(1, 6):
        try:
            lg = _timed_call(bs.login, HARD_TIMEOUT, "登录")
            if lg.error_code == "0":
                log("Baostock 登录成功（连通性探针 OK）")
                login_ok = True
                break
            if is_ban_error(lg.error_code, lg.error_msg):
                mark_banned(lg.error_code, lg.error_msg)
                sys.exit(3)
            log(f"  [WARN] Baostock 登录失败({attempt}/5): {lg.error_code} {lg.error_msg}")
        except Exception as _e:  # noqa: BLE001
            log(f"  [WARN] Baostock 登录异常({attempt}/5): {type(_e).__name__}: {str(_e)[:120]}")
        _force_close_bs_socket()  # 清掉半死连接，下次登录重新建
        time.sleep(min(30.0, 5.0 * attempt))
    if not login_ok:
        log("Baostock 连续 5 次登录失败 —— 请确认 Forest 代理已开启、SOCKS 端口 7891 在监听。")
        sys.exit(3)

    # 夹住结束日期：Baostock 分钟线常滞后 1 个交易日，而 kline 会先更新到「今天」。
    # 不夹住的话，断点续传会把起点算成今天 → 每只都只请求「今天~今天」→ 空数据 → 误判失败熔断。
    probe_last = None if args.end else probe_last_minute_date(args.period)
    if probe_last:
        log(f"数据源最近可用 {args.period} 分钟日期：{probe_last}（各只结束日期将夹到该日）")
    else:
        log("  [提示] 未探测到最近可用分钟线日期，将按 kline 末尾日期抓取")

    codes = sorted(p.stem for p in kdir.glob("*.parquet"))
    if args.limit:
        codes = codes[: args.limit]

    # 并行分片：把 codes 切成 N 片，本进程只跑其中第 shard 片
    if args.shards > 1:
        chunks = split_chunks(codes, args.shards)
        idx = max(1, min(args.shard, args.shards)) - 1
        codes = chunks[idx]
        log(f"分片 {args.shard}/{args.shards}：本分片 {len(codes)} 只（总 {len(chunks)} 片，共 {sum(len(c) for c in chunks)} 只）")

    state = {"done": 0, "ok": 0, "skip": 0, "fail": 0, "consec_fail": 0,
             "disabled": False, "disabled_at": 0.0, "start": time.time()}
    log(f"待处理 {len(codes)} 只 | 输入 {kdir} | 输出 {odir} | 周期 {args.period} | 间隔 {args.sleep}s | 起点 {args.start or START_DEFAULT}")

    for code in codes:
        if BANNED["hit"]:
            log("!! 出口 IP 已被 Baostock 拉黑（10001011），停止本分片剩余任务。")
            log("   请用 --proxy 走代理换出口重跑，或等封禁解除。已抓数据全部保留，重跑自动续传。")
            break
        if state["disabled"]:
            # 熔断后不退出进程：并行模式下退出会被看门狗反复重启，反而更浪费时间。
            # 改为冷却一段时间（每 30s 打一条心跳，避免被看门狗误判卡死）+ 重连，然后自动恢复。
            wait = DISABLE_TTL - (time.time() - state["disabled_at"])
            while wait > 0:
                log(f"数据源熔断中，冷却 {wait:.0f}s 后自动恢复 ...")
                time.sleep(min(30.0, wait))
                wait -= 30.0
            state["disabled"] = False
            state["consec_fail"] = 0
            if not relogin():
                log("  [WARN] 冷却结束仍无法重连 Baostock，结束本分片（已抓数据保留，重跑自动续传）")
                break
            log("已重新连接 Baostock，继续抓取 ...")
        s, e = code_range(code, kdir)
        if s is None:
            state["skip"] += 1
            log(f"- {code} 跳过（kline 无可用日期区间）")
            continue
        s, e = (args.start or START_DEFAULT), (args.end or e)
        if args.end is None and probe_last and e > probe_last:
            e = probe_last  # 夹到数据源真实可用的最后一天
        # 断点续传：若已有局部文件，从末尾日期之后继续，避免重复拉已抓的部分
        ex_last = existing_last_date(code, odir)
        if ex_last and ex_last >= s:
            s = (pd.Timestamp(ex_last) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if s > e:  # 已有文件已覆盖到目标末尾（或该股在起始日期前就退市/停牌，本就没有 2025 年后的数据）
            state["skip"] += 1
            if ex_last:
                log(f"- {code} 跳过（已有缓存已覆盖至 {ex_last}）")
            else:
                log(f"- {code} 跳过（该股 {e} 后无行情，区间内无数据）")
            continue
        if already_cached(code, s, e, odir):
            state["skip"] += 1
            log(f"- {code} 跳过（区间 {s}~{e} 已完整缓存）")
            continue

        log(f">> {code} 抓取区间 {s} ~ {e}")
        df = fetch_baostock(code, s, e, args.period, args.sleep)
        state["done"] += 1
        if df is None:
            state["fail"] += 1
            state["consec_fail"] += 1
            log(f"X {code} 失败 ({state['done']}/{len(codes)})")
            if state["consec_fail"] >= DISABLE_AFTER:
                state["disabled"] = True
                state["disabled_at"] = time.time()
                log(f"连续失败 {state['consec_fail']} 次 -> 熔断剩余任务（疑似网络不通/限频）")
            elif state["consec_fail"] >= 2:
                # 连续失败多半是会话被代理断开：尝试重连，避免整片停摆
                log(f"连续失败 {state['consec_fail']} 次，尝试重连 Baostock ...")
                relogin()
            continue

        if df.empty:
            # 没报错、只是该区间没有分钟数据（停牌/数据源尚未更新）→ 跳过，不计失败
            state["skip"] += 1
            state["consec_fail"] = 0
            log(f"- {code} 跳过（区间 {s}~{e} 内无数据）")
            continue

        # 合并到已有文件（续传），再去重排序
        ep = odir / f"{code}.parquet"
        if ep.exists():
            try:
                old = pd.read_parquet(ep)
                if not old.empty:
                    df = pd.concat([old, df], ignore_index=True)
            except Exception:  # noqa: BLE001
                pass
        df = df.drop_duplicates("date").sort_values("date")
        df.to_parquet(ep, index=False)
        state["consec_fail"] = 0
        state["ok"] += 1
        log(f"OK {code} 完成 写入 {len(df)} 行（ok={state['ok']} fail={state['fail']} skip={state['skip']}/{len(codes)}）")
        if state["done"] % 50 == 0:
            el = (time.time() - state["start"]) / 60
            log(f"进度 {state['done']}/{len(codes)} ok={state['ok']} fail={state['fail']} "
                f"skip={state['skip']} 用时 {el:.1f}min")

    try:
        bs.logout()
    except Exception:  # noqa: BLE001
        pass
    el = (time.time() - state["start"]) / 60
    log(f"完成 总{len(codes)} ok={state['ok']} fail={state['fail']} skip={state['skip']} 用时 {el:.1f}min")
    if state["disabled"]:
        log("提示：本次触发熔断，说明 Baostock 在当前环境不可达；请确认 Forest 代理已开、baostock.com 已加入 Forest 的 Proxy 规则。")


if __name__ == "__main__":
    main()
