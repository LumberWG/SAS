"""参数单一来源：把 ``Settings`` 的全部字段 + 关键硬编码常量整理成一份可读清单。

背景：同一个参数目前散在四个地方——`.env` / `config.py`、`app_state`（运行时覆盖）、
页面上的局部门开关、以及硬编码常量（黑名单、阈值、费率）。改一处忘一处就会出现
「开关关了没效果」这类 bug（见 WORKLOG 2026-10-30 那条）。

本模块只做两件事，都是**只读**的：
1. :func:`settings_overview` —— 把「当前生效值 + 来源 + 是否要重启」吐给
   `GET /api/settings` 与「系统设置」页；
2. 顺带把 :data:`RUNTIME_KEYS` 那份「允许运行时改」的白名单集中声明，
   后续把某些项做成可写时，只改这里。

不做的事：这里**不写**任何值（可写化是下一步），也不参与交易逻辑计算。
"""
from __future__ import annotations

import os
import re
from typing import Any

from app.config import BASE_DIR, Settings, get_settings

#: 允许运行时改（走 app_state 覆盖层，不用重启）的键。
#: 目前只有大盘股开关与模拟盘模式真正在跑这条路，其余仍是"改 .env + 重启"。
RUNTIME_KEYS: frozenset[str] = frozenset({
    "paper_exclude_large_cap",
    "paper_large_cap_yi",
    "paper_trade_mode",
})

#: 分组规则（按字段名前缀，先匹配先用）。命中不了的落到「其他」。
_GROUP_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("paper_",), "模拟交易与风控"),
    (("scan_", "fetch_", "kline_", "request_", "snapshot_", "proxy", "http_", "minute"), "数据源与扫描"),
    (("auto_watch_", "analysis_", "watch_", "focus_"), "分析与自选"),
    (("scheduler_", "notify_", "telegram_", "alert_", "monitor_"), "调度与通知"),
    (("llm_",), "LLM"),
    (("app_name", "debug", "db_", "kline_dir", "log_", "base_", "data_", "timezone"), "基础与存储"),
)

#: 分组展示顺序
GROUP_ORDER: tuple[str, ...] = (
    "基础与存储",
    "数据源与扫描",
    "分析与自选",
    "模拟交易与风控",
    "调度与通知",
    "LLM",
    "硬编码常量",
    "其他",
)

_FIELD_RE = re.compile(
    r"^    ([a-z_][a-z0-9_]*)\s*:\s*[^=]+=\s*(.+?)(?:\s{2,}#\s*(.+))?$"
)
_COMMENT_RE = re.compile(r"^\s{4}#\s?(.+?)\s*$")

#: 注释缺失或注释是分节标题时的兜底标签
_LABEL_OVERRIDES: dict[str, str] = {
    "app_name": "应用名称",
    "debug": "调试模式",
    "llm_base_url": "LLM 接口地址",
    "llm_api_key": "LLM 密钥",
    "llm_model": "LLM 模型",
    "llm_timeout": "LLM 超时",
    "llm_max_tokens": "LLM 最大输出 tokens",
    "auto_watch_top_n": "盘后自动加自选数量",
    "auto_scan_strategies": "盘后自动选股策略",
    "auto_scan_scope": "盘后选股范围",
    "shadow_start": "影子策略起点日期",
    "data_no_proxy_domains": "直连数据源域名（NO_PROXY）",
    "paper_half_score": "半仓评分下限（权重打 5 折）",
    "paper_scan_autotrade_top_n": "扫描命中自动交易上限",
    "paper_rotation_gap": "轮动所需评分差",
    "paper_backlog_max": "候补队列上限",
    "paper_large_cap_yi": "大盘股市值阈值（亿）",
    "paper_atr_stop_max": "ATR 止损上限",
    "paper_t_active": "做T总开关",
    "shadow_tag": "影子策略标签",
    "shadow_universe": "影子策略股票池规模",
}


def _label_map() -> dict[str, str]:
    """从 ``config.py`` 源码里抽出「字段 → 注释首行」当作中文标签。

    配置项的注释本来就写在字段上方（且是给人看的一句话说明），直接复用可以避免
    再维护一份标签表。取注释**首行**（第二行往后多是补充说明），分节分隔线
    （``# ---------- 基础 ----------``）必须跳过，否则标签会变成 "---------- 基础 ----------"。
    解析失败就退回字段名，不影响功能。
    """
    labels: dict[str, str] = dict(_LABEL_OVERRIDES)
    try:
        lines = (BASE_DIR / "backend" / "app" / "config.py").read_text(encoding="utf-8").splitlines()
    except OSError:
        return labels
    buf: list[str] = []
    for line in lines:
        comment = _COMMENT_RE.match(line)
        if comment:
            text = comment.group(1).strip()
            if text.startswith("-") or not set(text) - set("-= "):
                buf = []          # 分节分隔线，清空缓冲
                continue
            if not buf:
                buf.append(text)
            continue
        field = _FIELD_RE.match(line)
        if field:
            key = field.group(1)
            inline = (field.group(3) or "").strip()
            # 行尾注释优先（config.py 里做T/移动止损那批都是行尾一行说明）
            label = inline or (buf[0] if buf else "")
            if label and key not in labels:
                labels[key] = label
            buf = []
            continue
        if line.strip():
            buf = []
    return labels


def _group_of(key: str) -> str:
    for prefixes, group in _GROUP_RULES:
        if key.startswith(prefixes):
            return group
    return "其他"


def _env_keys() -> set[str]:
    """环境变量里显式设置过的大写键（含 `.env` 文件里的，pydantic 会读它）。"""
    keys = {k.upper() for k in os.environ}
    try:
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            keys.add(text.split("=", 1)[0].strip().upper())
    except OSError:
        pass
    return keys


def _jsonable(value: Any) -> Any:
    from pathlib import Path

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def _coerce(raw: str, default: Any) -> Any:
    """app_state 里存的是字符串，按默认值类型转回来，前端才好统一渲染。"""
    if isinstance(default, bool):
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(float(raw))
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def _builtin_items() -> list[dict]:
    """硬编码口径常量：改它们要动代码，页面上只读展示，避免"以为能调"的误会。"""

    def grab(module: str, name: str, label: str, unit: str = "", fmt=None) -> dict | None:
        try:
            mod = __import__(module, fromlist=[name])
            value = getattr(mod, name)
        except Exception:  # noqa: BLE001 - 常量缺失不该让整页 500
            return None
        shown = fmt(value) if fmt else value
        return {
            "key": f"{module}.{name}",
            "label": label,
            "value": _jsonable(shown),
            "unit": unit,
            "source": "code",
            "restart_required": True,
            "writable": False,
            "note": "硬编码在源码里，改需重新发布",
        }

    items = [
        grab("app.analysis.factors", "BUY_THRESHOLD", "买入评分阈值（≥ 即买入）", "分"),
        grab("app.analysis.factors", "SELL_THRESHOLD", "卖出评分阈值（≤ 即卖出）", "分"),
        grab("app.backtest.pipeline", "STOP_LOSS_RATIO", "回测默认止损", "", lambda v: round(v * 100, 2)),
        grab("app.backtest.pipeline", "TAKE_PROFIT_RATIO", "回测默认止盈", "", lambda v: round(v * 100, 2)),
        grab("app.paper.engine", "SINGLE_POSITION_MAX_RATIO", "单票占总资产上限", "%", lambda v: round(v * 100, 1)),
        grab("app.paper.engine", "LOT_SIZE", "整手股数", "股"),
        grab("app.paper.engine", "FEE_COMMISSION_RATE", "佣金费率（双向）", "%", lambda v: round(v * 10000, 2)),
        grab("app.paper.engine", "FEE_STAMP_RATE", "印花税（卖出）", "%", lambda v: round(v * 1000, 2)),
        grab("app.paper.engine", "FEE_TRANSFER_RATE", "过户费（双向）", "%", lambda v: round(v * 10000, 2)),
        grab("app.data.scope", "MAIN_BOARD_PREFIXES", "可交易板块前缀", "", lambda v: " / ".join(v)),
        grab("app.data.scope", "GIANT_CODES", "巨无霸黑名单只数", "只", len),
    ]
    return [i for i in items if i]


def settings_overview() -> dict:
    """当前**生效值**总览：按分组返回全部参数 + 来源 + 生效方式。"""
    s = get_settings()
    labels = _label_map()
    env_keys = _env_keys()
    fields = Settings.model_fields
    from app.data.cache import db

    groups: dict[str, list[dict]] = {}
    for key, field in fields.items():
        default = field.default
        value = getattr(s, key, default)
        source = "default"
        if key in RUNTIME_KEYS:
            raw = None
            try:
                raw = db.get_state(key)
            except Exception:  # noqa: BLE001
                raw = None
            if raw is not None:
                value = _coerce(raw, default)
                source = "app_state"
        if source == "default" and key.upper() in env_keys:
            source = "env"
        runtime = key in RUNTIME_KEYS
        groups.setdefault(_group_of(key), []).append({
            "key": key,
            "label": labels.get(key) or key,
            "value": _jsonable(value),
            "type": "bool" if isinstance(default, bool)
            else "int" if isinstance(default, int)
            else "float" if isinstance(default, float)
            else "str",
            "source": source,
            "restart_required": not runtime,
            "writable": False,          # 第一步只读；可写化见工作日志
            "runtime": runtime,
            "note": "",
        })

    builtin = _builtin_items()
    if builtin:
        groups.setdefault("硬编码常量", []).extend(builtin)

    ordered = [
        {"name": name, "items": groups[name]}
        for name in GROUP_ORDER
        if name in groups
    ]
    total = sum(len(g["items"]) for g in ordered)
    runtime_count = sum(1 for g in ordered for i in g["items"] if i["source"] == "app_state")
    return {
        "groups": ordered,
        "total": total,
        "runtime_overrides": runtime_count,
        "hint": "只读总览：当前值 = app_state 覆盖 > 环境变量/.env > 默认值。"
                "标「需重启」的项改了要重启后端才生效。",
    }
