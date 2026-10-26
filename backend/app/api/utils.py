"""API 公共工具。"""
from __future__ import annotations

from app.config import get_settings


def with_disclaimer(data: dict) -> dict:
    """统一注入免责声明字段（合规要求）。"""
    data["disclaimer"] = get_settings().disclaimer
    return data


def with_realtime(items: list[dict]) -> list[dict]:
    """给列表条目附加当前实时行情（``realtime`` 字段）。

    DB / 任务里保存的是「记录时刻」的行情（选股命中价、建议价），
    列表展示应以**当前**行情为准，否则刷新页面看到的还是几分钟前
    （甚至昨天）的价格。实时快照不可用时原样返回，前端回退到记录值。

    Args:
        items: 每项需含 ``code``（6位代码）

    Returns:
        新列表（不修改入参，避免污染内存任务/持久化对象）；
        附带的 ``realtime`` 结构为 ``{price, pct_change, volume_ratio, amount_yi}``。
    """
    if not items:
        return items
    from app.data.snapshot_store import quote_map

    quotes = quote_map()
    if not quotes:
        return items

    def _round(value, digits: int = 2):
        return None if value is None else round(float(value), digits)

    out: list[dict] = []
    for item in items:
        quote = quotes.get(str(item.get("code")))
        if quote is None:
            out.append(item)
            continue
        enriched = dict(item)
        enriched["realtime"] = {
            "price": round(float(quote["price"]), 2),
            "pct_change": _round(quote["pct_change"]),
            "volume_ratio": _round(quote["volume_ratio"]),
            "amount_yi": _round(None if quote["amount"] is None else quote["amount"] / 1e8),
        }
        out.append(enriched)
    return out
