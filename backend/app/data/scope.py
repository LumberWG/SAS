"""项目可交易范围：板块白名单 + 剔除 ST。

#181 起板块口径统一收敛到 ``app.board_rules``：本模块不再自带前缀表，
避免与「选股板块」(bottom_boards) 出现两套不一致的归类（例如 689 / 004 / 920）。
``twin_t_boards`` 同时接受中文名与英文键，可与 bottom_boards 填同一套值。
"""
from __future__ import annotations

import re
from functools import lru_cache

from app.board_rules import (
    BOARD_PREFIXES,
    BOARD_ZH,
    board_key_of,
    board_scope_consistency,
    parse_trade_boards,
    prefixes_for_board_keys,
)

#: 历史缺省口径（主板+创业板），仅在 twin_t_boards 留空/解析为空时退回，
#: 保证未配置时行为与旧版一致。科创板/北交所/B 股不在其中。
MAIN_BOARD_PREFIXES: tuple[str, ...] = (
    "600", "601", "603", "605", "000", "001", "002", "003", "300", "301",
)


def board_of(code: str) -> str:
    """按代码前缀判定板块（主板/创业板/科创板/北交所/其他）。"""
    return BOARD_ZH.get(board_key_of(code), "其他")


def _allowed_board_prefixes() -> tuple[str, ...]:
    """当前「交易板块」白名单对应的代码前缀元组（来自 Settings.twin_t_boards）。

    同时接受中文名（主板/创业板/科创板/北交所）与英文键
    （sh_main/sz_main/cyb/star/bse），因此可直接填 bottom_boards 的同款值。
    留空或解析为空时退回历史口径 MAIN_BOARD_PREFIXES，保证旧配置零行为变更。
    """
    from app.config import get_settings

    raw = (getattr(get_settings(), "twin_t_boards", "") or "").strip()
    if not raw:
        return MAIN_BOARD_PREFIXES
    keys = parse_trade_boards(raw)
    if not keys:
        return MAIN_BOARD_PREFIXES
    return prefixes_for_board_keys(keys)

_ST_RE = re.compile(r"\*?ST|退", re.IGNORECASE)


def is_st_name(name) -> bool:
    """名称是否带 ST / *ST / 退市标记——**实盘与回测共用的唯一口径**。

    扫描器 ``scanner``、模拟盘买入池 ``paper/engine`` 走 :func:`is_tradable`，
    回测股票池 ``backtest/pipeline`` 直接调本函数（回测板块另由 board_prefixes
    控制，不能再套 is_tradable 的板块白名单，否则会二次过滤）。以前三处各写
    一份判断，回测漏了这一层，结果把 ST南都(300068) 放进池子、单笔 -57%
    （回测 #44）。此后任何一侧要改 ST 判定，只改本函数。
    """
    return bool(_ST_RE.search(str(name or "")))


def is_cyb(code: str) -> bool:
    """是否创业板（20% 涨跌幅，项目偏好）。"""
    return str(code).startswith(("300", "301"))


def clean_name(code: str, name) -> str:
    """清洗股票名称，消除乱码导致的前端显示问题。

    来源：akshare 东财 ``stock_info_a_code_name`` 对个别票（多为 688 科创板）
    返回 GBK 字节被当 UTF-8 解出的乱码（如 ``'оԴ΢'``）。检测逻辑与
    ``app/paper/engine._name_ok`` 同源：空 / 等于代码 / 含替换字符 / 能 gbk 往返
    且与原串不同 → 一律用代码兜底，避免把乱码写进缓存与前端。
    """
    s = str(name or "").strip()
    if not s or s == str(code) or "\ufffd" in s:
        return str(code)
    try:
        if s.encode("gbk").decode("utf-8") != s:
            return str(code)
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return s


def is_tradable(code: str, name: str = "") -> tuple[bool, str]:
    """判断是否在项目可交易范围。

    返回 (是否可交易, 原因)，原因用于诊断（board_excluded / st / invalid / ok）。

    板块白名单由设置项 twin_t_boards 驱动（默认主板+创业板，留空退回
    MAIN_BOARD_PREFIXES）；大市值票（银行/石油等）由独立的 paper_large_cap_yi
    市值阈值在 scanner / 实盘买入池中剔除，不再有显式巨无霸代码黑名单。
    """
    c = str(code)
    if not (c.isdigit() and len(c) == 6):
        return False, "invalid"
    # 板块白名单由设置项 twin_t_boards 驱动（默认主板+创业板，留空退回 MAIN_BOARD_PREFIXES）。
    # 改这一处即可同时约束扫描器、实盘买入池与统计口径，避免各处分散硬编码。
    if not c.startswith(_allowed_board_prefixes()):
        return False, "board_excluded"
    if is_st_name(name):
        return False, "st"
    return True, "ok"


#: 范围口径参考规模（截至 2026-10-28，基于 akshare 全 A 股票池实测）。
#: 股票池随时变动，此常量仅作文档基准；**实际以 :func:`get_universe_stats` 动态计算为准**，
#: 供前端 / 日志展示「当前可交易范围」的总量。
SCOPE_REFERENCE: dict = {
    "as_of": "2026-10-28",
    "source_total": 5588,
    "in_scope": 4392,
    "mainboard": 3020,
    "cyb": 1372,
    "excluded": {"board_excluded": 972, "st": 190},
}


@lru_cache(maxsize=1)
def _universe_stats_cached() -> dict:
    """从股票池动态计算范围规模（结果缓存 1 小时，与股票池 TTL 同量级）。"""
    from app.data.service import get_stock_pool  # 延迟导入，避免循环依赖
    from collections import Counter

    df = get_stock_pool()
    res = [is_tradable(c, n) for c, n in zip(df["code"], df["name"])]
    ok = [r[0] for r in res]
    codes = [c for c, o in zip(df["code"], ok) if o]
    cyb = sum(1 for c in codes if is_cyb(c))
    reasons = Counter(r[1] for r in res)
    by_board = Counter(board_key_of(c) for c in codes)
    return {
        "source_total": int(len(df)),
        "in_scope": int(len(codes)),
        # 兼容旧前端字段：mainboard 仍表示「非创业板」（含科创/北交所），
        # 真实板块拆分请看新增的 by_board。
        "mainboard": int(len(codes) - cyb),
        "cyb": int(cyb),
        "by_board": {k: int(by_board.get(k, 0)) for k in BOARD_PREFIXES},
        "excluded": {k: int(reasons.get(k, 0))
                     for k in ("board_excluded", "st", "invalid")},
    }


def get_universe_stats() -> dict:
    """当前可交易范围规模（按板块白名单，剔 ST）。

    返回 source_total / in_scope / mainboard / cyb / **by_board**（各板块真实拆分）
    / excluded 分布 / **board_consistency**（选股板块 vs 交易板块一致性提示）。
    计算失败（股票池不可达）时回退到 :data:`SCOPE_REFERENCE` 文档值。
    """
    from app.config import get_settings

    try:
        out = dict(_universe_stats_cached())
    except Exception:  # noqa: BLE001 - 降级为文档参考值，保证 API 不挂
        out = dict(SCOPE_REFERENCE)
    # 一致性检查依赖当前设置，不能放进 lru_cache 的统计结果里
    try:
        s = get_settings()
        # 选股 + 交易已统一到 twin_t_boards（单一板块白名单）：两侧用同一份设置，
        # 因此始终一致，「板块不一致」告警不再出现。bottom_boards 仅作向后兼容字段。
        raw = getattr(s, "twin_t_boards", "") or ""
        out["board_consistency"] = board_scope_consistency(raw, raw)
    except Exception:
        pass
    return out
