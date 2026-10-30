# -*- coding: utf-8 -*-
"""修正 free-stockdb SDK 的 5 分钟聚合 bug。

原版 `StockDBClient._merge_minutes_to_period` 里有：

    high = max(x['high'] for x in items if 'high' in x)
    low  = min(x['low']  for x in items if 'low'  in x)

只判断了 key 是否存在，没判断值是否为 None。遇到停牌/无成交的分钟（库里
high/low 为 None）就会抛：

    TypeError: '>' not supported between instances of 'NoneType' and 'float'

实测 002762、002719、002856、002898、002931 等 50+ 只股票因此完全取不到
数据（本地库其实有完整 21120 根）。本模块提供容错版聚合，数值缺失时跳过
该样本、用同组有效值兜底，行数与原版一致。

用法：
    import fsdb_safe_merge
    fsdb_safe_merge.apply()
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

_APPLIED = False


def _num_list(items: List[Dict[str, Any]], key: str) -> List[float]:
    out: List[float] = []
    for x in items:
        v = x.get(key)
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _pick(item: Dict[str, Any], key: str) -> Optional[float]:
    v = item.get(key)
    return float(v) if isinstance(v, (int, float)) else None


def _merge_safe(self, minute_data: List[Dict[str, Any]], frequency: str) -> List[Dict[str, Any]]:
    """安全版分钟聚合：容忍 high/low/open/close 为 None 的分钟。"""
    if not minute_data:
        return []

    interval = int(frequency[:-1])

    def trading_elapsed(minute_of_day: int) -> Optional[int]:
        if 570 <= minute_of_day <= 690:
            return minute_of_day - 570
        if 780 <= minute_of_day <= 900:
            if minute_of_day == 780:
                return 121
            return 120 + (minute_of_day - 780)
        return None

    def elapsed_to_minute_of_day(elapsed: int) -> int:
        if elapsed <= 120:
            return 570 + elapsed
        if elapsed > 240:
            elapsed = 240
        return 780 + (elapsed - 120)

    grouped: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for item in minute_data:
        date_val = item.get("date")
        if not date_val:
            continue
        try:
            date_int = int(date_val)
        except (TypeError, ValueError):
            continue
        if date_int < 10000000000000:
            continue
        ymd = date_int // 1000000
        hour = (date_int // 10000) % 100
        minute = (date_int // 100) % 100
        elapsed = trading_elapsed(hour * 60 + minute)
        if elapsed is None:
            continue
        if elapsed <= 0:
            group_end_elapsed = interval
        else:
            group_idx = (elapsed - 1) // interval
            group_end_elapsed = (group_idx + 1) * interval
        grouped[(ymd, group_end_elapsed)].append(item)

    merged_list: List[Dict[str, Any]] = []
    for idx, key in enumerate(sorted(grouped.keys())):
        ymd, end_elapsed = key  # 注意：必须从 key 解出 ymd，否则会用到循环残留值（全变成最后一天）
        items = grouped[key]
        first_item = items[0]
        last_item = items[-1]

        open_v = _pick(first_item, "open")
        close_v = _pick(last_item, "close")
        fallback = open_v if open_v is not None else close_v
        if fallback is None:
            fallback = 0.0
        if open_v is None:
            open_v = close_v if close_v is not None else fallback
        if close_v is None:
            close_v = open_v

        highs = _num_list(items, "high")
        lows = _num_list(items, "low")
        high = max(highs) if highs else fallback
        low = min(lows) if lows else fallback
        volume = sum(_num_list(items, "volume"))
        amount = sum(_num_list(items, "amount"))

        end_minute_of_day = elapsed_to_minute_of_day(end_elapsed)
        end_hour = end_minute_of_day // 60
        end_minute = end_minute_of_day % 60
        if end_hour >= 24:
            end_hour, end_minute = 23, 59

        merged_item: Dict[str, Any] = {
            "date": ymd * 1000000 + end_hour * 10000 + end_minute * 100,
            "code": last_item.get("code"),
            "name": last_item.get("name", ""),
            "open": open_v,
            "high": high,
            "low": low,
            "close": close_v,
            "volume": volume,
            "amount": amount,
        }

        if idx > 0:
            pre_close = merged_list[-1]["close"]
        else:
            pre_close = _pick(first_item, "pre_close") or open_v
        merged_item["pre_close"] = pre_close

        if pre_close:
            merged_item["pct_chg"] = round(((close_v - pre_close) / pre_close) * 100, 3)
            merged_item["amplitude"] = round(((high - low) / pre_close) * 100, 3)
        else:
            merged_item["pct_chg"] = 0.0
            merged_item["amplitude"] = 0.0

        for field in ("vol_ratio", "pe_ttm", "pb", "total_mv", "float_mv",
                      "float_share", "total_share", "is_st"):
            if field in last_item:
                merged_item[field] = last_item[field]

        merged_list.append(merged_item)

    return merged_list


def apply() -> None:
    """给已经导入的 stock_sdk.StockDBClient 打补丁（幂等）。"""
    global _APPLIED
    if _APPLIED:
        return
    from stock_sdk import StockDBClient  # noqa: WPS433

    StockDBClient._merge_minutes_to_period = _merge_safe
    _APPLIED = True
