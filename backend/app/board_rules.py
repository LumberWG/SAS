"""A 股板块交易规则（统一口径，纯函数、零重依赖）。

背景：系统原先按「主板 + 创业板」设计，撮合层把涨跌停写死 ±9.5%、
交易单位写死 100 股整手；而设置里已允许把选股/交易切到科创板、北交所。
本模块把「代码前缀 → 板块 → 交易规则」收敛成单一来源，供 scope / 撮合 /
回测 / 行情统计共用，避免各处散落硬编码。

规则（2026-11）：
- 涨跌停：主板 ±10%、创业板/科创板 ±20%、北交所 ±30%、ST ±5%。
- 申报数量：主板/创业板 100 股整手；科创板 200 股起、1 股递增；
  北交所 100 股起、1 股递增。
- 前缀表统一了原先散落在 ``app.data.scope`` 与
  ``app.analysis.board_index.BOARD_INDEX`` 的两套口径（补齐 689、004、920 等差异）。
- 刻意放在 ``app`` 顶层而非 ``app.analysis`` 下：后者 ``__init__.py`` 会拉起
  recommender → data.service → scope，供 scope 导入时会产生循环依赖。

所有函数只读、不抛异常；``legacy_limit=True`` 可一键回到旧的全市场 ±9.5%
口径，用于复现历史回测基线。
"""
from __future__ import annotations

from functools import lru_cache

#: 板块键 → 代码前缀。顺序即匹配优先级（互不重叠，但显式列出便于维护）。
BOARD_PREFIXES: dict[str, tuple[str, ...]] = {
    "sh_main": ("600", "601", "603", "605"),                 # 沪市主板
    "sz_main": ("000", "001", "002", "003", "004"),          # 深市主板
    "cyb": ("300", "301"),                                  # 创业板
    "star": ("688", "689"),                                 # 科创板（689=CDR）
    "bse": ("43", "83", "87", "88", "920"),                 # 北交所
}

#: 板块键 → 中文名（与 twin_t_boards 的取值对齐）
BOARD_ZH: dict[str, str] = {
    "sh_main": "主板", "sz_main": "主板", "cyb": "创业板",
    "star": "科创板", "bse": "北交所",
}

#: 交易板块设置里出现的中文名 → 板块键（「主板」同时含沪深主板）
ZH_TO_BOARD_KEYS: dict[str, tuple[str, ...]] = {
    "主板": ("sh_main", "sz_main"),
    "创业板": ("cyb",),
    "科创板": ("star",),
    "北交所": ("bse",),
}

#: 板块键 → (涨跌停幅度 %, 最小申报股数, 递增步长)
_BOARD_RULE: dict[str, tuple[float, int, int]] = {
    "sh_main": (10.0, 100, 100),
    "sz_main": (10.0, 100, 100),
    "cyb": (20.0, 100, 100),
    "star": (20.0, 200, 1),
    "bse": (30.0, 100, 1),
}
_DEFAULT_RULE: tuple[float, int, int] = (10.0, 100, 100)

#: 涨跌停判定容差（百分点）。旧口径 9.5% = 10% − 0.5%，保留同一容差语义。
#: 用「接近涨停价」而非精确涨停价，避免复权/四舍五入误差把边缘交易判死。
LIMIT_TOLERANCE_PCT = 0.5


def board_key_of(code: str) -> str:
    """按代码前缀判定板块键：sh_main/sz_main/cyb/star/bse，未知返回 ``other``。"""
    c = str(code).strip()
    for key, prefixes in BOARD_PREFIXES.items():
        if c.startswith(prefixes):
            return key
    return "other"


def prefixes_for_board_keys(keys) -> tuple[str, ...]:
    """板块键集合 → 代码前缀元组（供 scope 白名单复用，消除两套前缀表差异）。"""
    out: list[str] = []
    for k in keys:
        k = str(k).strip()
        if k in BOARD_PREFIXES:
            out.extend(BOARD_PREFIXES[k])
        elif k in ZH_TO_BOARD_KEYS:  # 也接受中文名
            for bk in ZH_TO_BOARD_KEYS[k]:
                out.extend(BOARD_PREFIXES[bk])
    return tuple(dict.fromkeys(out))


def parse_trade_boards(raw: str) -> tuple[str, ...]:
    """解析 ``twin_t_boards`` 设置值，返回板块键元组。

    同时接受中文名（主板/创业板/科创板/北交所）与英文键
    （sh_main/sz_main/cyb/star/bse），让「选股板块」和「交易板块」
    两套设置可以填同一套值。
    """
    out: list[str] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part in ZH_TO_BOARD_KEYS:
            out.extend(ZH_TO_BOARD_KEYS[part])
        elif part in BOARD_PREFIXES:
            out.append(part)
    return tuple(dict.fromkeys(out))


def limit_pct(code: str, name: str = "", legacy_limit: bool = False) -> float:
    """涨跌停幅度（%）。``legacy_limit=True`` 强制返回 10（旧全市场口径）。"""
    if legacy_limit:
        return 10.0
    if "ST" in str(name or "").upper() or "退" in str(name or ""):
        return 5.0
    return _BOARD_RULE.get(board_key_of(code), _DEFAULT_RULE)[0]


def limit_up_ratio(code: str, name: str = "", legacy_limit: bool = False) -> float:
    """涨停判定比例：``prev_close × 此值`` 视为接近涨停（主板 1.095、创业板 1.195…）。"""
    return 1.0 + (limit_pct(code, name, legacy_limit) - LIMIT_TOLERANCE_PCT) / 100.0


def limit_down_ratio(code: str, name: str = "", legacy_limit: bool = False) -> float:
    """跌停判定比例：``prev_close × 此值`` 视为接近跌停（主板 0.905、北交所 0.705…）。"""
    return 1.0 - (limit_pct(code, name, legacy_limit) - LIMIT_TOLERANCE_PCT) / 100.0


def lot_rule(code: str) -> tuple[int, int]:
    """返回 ``(最小申报股数, 递增步长)``。

    主板/创业板 (100,100)、科创板 (200,1)、北交所 (100,1)。
    """
    return _BOARD_RULE.get(board_key_of(code), _DEFAULT_RULE)[1:]


def round_buy_qty(code: str, qty: float) -> int:
    """把拟买入股数按板块申报规则向下取整；不足最小申报量返回 0。"""
    try:
        n = int(float(qty))
    except (TypeError, ValueError):
        return 0
    min_qty, step = lot_rule(code)
    if n < min_qty:
        return 0
    return (n // step) * step if step > 1 else n


def round_sell_qty(code: str, qty: float, held: int | None = None) -> int:
    """把拟卖出股数按板块申报规则向下取整。

    整手板块若持仓本身不足一手（送转产生的零股），允许一次性卖光；
    1 股递增板块（科创/北交所）不做整手取整，可直接卖净零股。
    """
    try:
        n = int(float(qty))
    except (TypeError, ValueError):
        return 0
    min_qty, step = lot_rule(code)
    if step == 1:
        n = min(n, int(held)) if held is not None else n
        return max(0, n)
    n = (n // step) * step
    if n == 0 and held is not None and 0 < int(held) < min_qty:
        n = int(held)  # 零股余仓一次卖光
    if held is not None:
        n = min(n, int(held))
    return max(0, n)


@lru_cache(maxsize=1)
def exit_scaling_enabled() -> bool:
    """是否按板块涨跌幅缩放底反策略的止盈目标（``bottom_scale_exit_by_limit``）。"""
    try:
        from app.config import get_settings
        return bool(getattr(get_settings(), "bottom_scale_exit_by_limit", False))
    except Exception:
        return False


def exit_scale_factor(code: str) -> float:
    """止盈幅度缩放因子，基准 = 创业板 ±20%。

    主板 0.5（+25% → +12.5%，一个板可达）、创业板/科创板 1.0（不变）、
    北交所 1.5（+37.5%）。clamp 到 [0.25, 2.0] 防极端值。
    """
    return max(0.25, min(2.0, limit_pct(code) / 20.0))


@lru_cache(maxsize=1)
def legacy_limit_enabled() -> bool:
    """是否启用旧的全市场 ±9.5% 涨跌停口径（``board_rules_legacy_limit``）。

    带进程内缓存：``get_settings()`` 会构造 Settings 并做目录检查，撮合热路径
    每天每票判定数十次，不能每次都读。改此开关后需重启服务（或调用
    ``legacy_limit_enabled.cache_clear()``）生效。
    """
    try:
        from app.config import get_settings
        return bool(getattr(get_settings(), "board_rules_legacy_limit", False))
    except Exception:
        return False


def board_scope_consistency(trade_boards_raw: str, scan_boards_raw: str) -> dict:
    """对比「交易板块」与「选股板块」，给出不一致警告（供设置页/API 提示）。

    返回 ``{ok, trade_boards, scan_boards, missing_in_trade, extra_in_trade, message}``。
    ``missing_in_trade`` 非空意味着：选股会给出这些板块的候选，但买入侧会全部拒掉。
    """
    trade = set(parse_trade_boards(trade_boards_raw))
    scan = {b.strip() for b in str(scan_boards_raw or "").split(",")
            if b.strip() and b.strip() in BOARD_PREFIXES}
    missing = sorted(scan - trade)
    extra = sorted(trade - scan)
    def zh(ks) -> str:
        # 沪深主板共用中文名，去重避免「主板、科创板、主板」这种重复展示
        return "、".join(dict.fromkeys(BOARD_ZH.get(k, k) for k in ks))
    msg = ""
    if missing:
        msg = (f"选股板块含 {zh(missing)}，但交易板块未勾选 → 这些板块的候选会被买入侧拒绝"
               f"（卖出/持仓不受影响）。请在设置里同步勾选「交易板块」。")
    return {
        "ok": not missing,
        "trade_boards": sorted(trade),
        "scan_boards": sorted(scan),
        "missing_in_trade": missing,
        "extra_in_trade": extra,
        "message": msg,
    }


__all__ = [
    "BOARD_PREFIXES", "BOARD_ZH", "ZH_TO_BOARD_KEYS", "LIMIT_TOLERANCE_PCT",
    "board_key_of", "prefixes_for_board_keys", "parse_trade_boards",
    "limit_pct", "limit_up_ratio", "limit_down_ratio", "lot_rule",
    "round_buy_qty", "round_sell_qty", "board_scope_consistency",
    "legacy_limit_enabled", "exit_scaling_enabled", "exit_scale_factor",
]
