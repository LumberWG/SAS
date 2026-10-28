"""项目可交易范围：沪深主板 + 创业板，剔除 ST 与巨无霸（银行/石油/保险）。

与 ``app/backtest/pipeline.MAIN_BOARD_PREFIXES`` 同一口径，抽成独立模块供
数据源、分析脚本、回测统一复用，避免各处硬编码重复。
"""
from __future__ import annotations

import re

#: 沪深主板 + 创业板。科创板(688)/北交所(920,43,83,87)/B 股(900,200) 不进池——
#: 否则结论可能建立在买不到或涨跌幅受限的票上。
MAIN_BOARD_PREFIXES: tuple[str, ...] = (
    "600", "601", "603", "605", "000", "001", "002", "003", "300", "301",
)

#: 巨无霸黑名单：银行 / 石油 / 保险 / 超大盘权重股。项目不碰（波动小、弹性低，
#: 且占成交额池会稀释小盘信号）。按需扩充。
GIANT_CODES: frozenset[str] = frozenset({
    # 银行
    "601398", "601939", "601288", "601988", "600036", "600000", "601328",
    "601166", "600016", "601169", "600928", "601229", "601818", "601658",
    "600919", "601077", "002142", "600015", "601009",
    # 石油 / 石化
    "601857", "600028", "600938", "601808",
    # 保险 / 券商权重
    "601318", "601628", "601601", "600030", "600837",
    # 其他超大盘（白酒 / 光伏 / 水电 / 免税 / 药明等）
    "600519", "000858", "601012", "600900", "601888", "600276", "000333",
})

_ST_RE = re.compile(r"\*?ST|退", re.IGNORECASE)


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

    返回 ``(是否可交易, 原因)``，原因用于诊断（board_excluded / giant / st /
    invalid / ok）。
    """
    c = str(code)
    if not (c.isdigit() and len(c) == 6):
        return False, "invalid"
    if not c.startswith(MAIN_BOARD_PREFIXES):
        return False, "board_excluded"
    if c in GIANT_CODES:
        return False, "giant"
    if _ST_RE.search(str(name or "")):
        return False, "st"
    return True, "ok"
