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

import json
import logging
import os
import re
from typing import Any

from app.config import BASE_DIR, Settings, get_settings

logger = logging.getLogger("sas.param_specs")

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

#: 页面不可编辑的键：路径 / 列表类。它们在进程启动时就被各处捕获（静态挂载、CORS、
#: 缓存目录），运行中改只会造成「显示改了、实际没变」的错觉，不如明确拒绝。
READONLY_KEYS: frozenset[str] = frozenset({
    "kline_dir",
    "db_path",
    "cors_origins",
    "data_no_proxy_domains",
    "auto_scan_strategies",
})

#: 运行时覆盖在 app_state 里的前缀（`RUNTIME_KEYS` 那几项用无前缀的既有键）
OVERRIDE_PREFIX = "cfg:"

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


def kind_of(key: str, default: Any) -> str:
    """页面渲染用的控件类型：``bool`` 开关 / ``number`` 数字框 / ``text`` 文本框 / ``readonly`` 只读。"""
    if key in READONLY_KEYS:
        return "readonly"
    if isinstance(default, bool):
        return "bool"
    if isinstance(default, (int, float)):
        return "number"
    return "text"


def _coerce_value(key: str, raw: Any, default: Any) -> Any:
    """把前端/字符串输入转成字段该有的类型（类型不合法直接抛 ValueError）。"""
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on", "开", "是"):
            return True
        if text in ("0", "false", "no", "off", "关", "否", ""):
            return False
        raise ValueError(f"{key} 需要布尔值，收到 {raw!r}")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(round(float(raw)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要整数，收到 {raw!r}") from exc
    if isinstance(default, float):
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} 需要数字，收到 {raw!r}") from exc
        if value != value:      # NaN
            raise ValueError(f"{key} 不合法（NaN）")
        return value
    return "" if raw is None else str(raw)


def update_settings(changes: dict[str, Any]) -> dict[str, Any]:
    """批量修改配置：写 app_state 覆盖层 + **就地**改 ``get_settings()`` 单例。

    就地改单例是关键：几乎所有代码都持有 ``self.settings`` 这个对象引用，
    替换对象无效；改属性才能让「下一个 tick / 下一轮扫描」立刻用上新值。
    覆盖同时落库，重启后由 :func:`apply_persisted_overrides` 重新套用。

    抛 ``ValueError`` 表示该键不可改或值类型不合法，由 API 层转 400。
    """
    from app.data.cache import db

    s = get_settings()
    applied: dict[str, Any] = {}
    for key, raw in changes.items():
        field = Settings.model_fields.get(key)
        if field is None:
            raise ValueError(f"未知参数: {key}")
        if key in READONLY_KEYS:
            raise ValueError(f"{key} 不支持页面修改（路径/列表类，请改 .env 后重启）")
        value = _coerce_value(key, raw, field.default)
        target = key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key
        db.set_state(target, "1" if value is True else "0" if value is False else str(value))
        setattr(s, key, value)
        applied[key] = value
    if applied:
        logger.info("系统设置已更新: %s", ", ".join(sorted(applied)))
    return applied


def reset_settings(keys: list[str] | None = None) -> list[str]:
    """还原为 `.env` / 环境变量的值：删覆盖 + 用新建的 ``Settings()`` 回填。

    ``keys`` 为空表示还原全部可编辑项。
    """
    from app.data.cache import db

    s = get_settings()
    fresh = Settings()
    targets = list(keys) if keys else [
        k for k in Settings.model_fields if k not in READONLY_KEYS
    ]
    done: list[str] = []
    for key in targets:
        if key not in Settings.model_fields or key in READONLY_KEYS:
            continue
        try:
            db.delete_state(key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("删除覆盖失败 %s: %s", key, exc)
        setattr(s, key, getattr(fresh, key))
        done.append(key)
    if done:
        logger.info("系统设置已还原 %d 项", len(done))
    return done


def apply_persisted_overrides() -> int:
    """启动时把 app_state 里的覆盖重新套到 Settings 单例上（否则重启即丢失）。"""
    from app.data.cache import db

    s = get_settings()
    applied = 0
    for key, field in Settings.model_fields.items():
        if key in READONLY_KEYS:
            continue
        try:
            raw = db.get_state(key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取覆盖失败 %s: %s", key, exc)
            continue
        if raw is None or raw == "":
            continue
        try:
            setattr(s, key, _coerce_value(key, raw, field.default))
            applied += 1
        except ValueError:
            logger.warning("忽略非法配置覆盖 %s=%r", key, raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("应用配置覆盖失败 %s: %s", key, exc)
    if applied:
        logger.info("已应用 %d 项运行时配置覆盖（来自 app_state）", applied)
    return applied


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
        # 覆盖层的键名有两套：RUNTIME_KEYS 沿用既有无前缀键（engine 直接读它们），
        # 其余走 `cfg:` 前缀，避免与业务自定义状态键撞名
        state_key = key if key in RUNTIME_KEYS else OVERRIDE_PREFIX + key
        try:
            raw = db.get_state(state_key)
        except Exception:  # noqa: BLE001
            raw = None
        if raw is not None and raw != "":
            try:
                value = _coerce_value(key, raw, default)
                source = "app_state"
            except ValueError:
                logger.warning("配置项 %s 的覆盖值非法: %r", key, raw)
        if source == "default" and key.upper() in env_keys:
            source = "env"
        runtime = key in RUNTIME_KEYS
        kind = kind_of(key, default)
        groups.setdefault(_group_of(key), []).append({
            "key": key,
            "label": labels.get(key) or key,
            "value": _jsonable(value),
            "type": "bool" if isinstance(default, bool)
            else "int" if isinstance(default, int)
            else "float" if isinstance(default, float)
            else "str",
            "source": source,
            # 可编辑项写 app_state 覆盖层并就地生效，重启后由启动流程重放，所以不用重启；
            # 路径/列表类标「需重启」，页面上不给输入框。
            "restart_required": kind == "readonly",
            "writable": kind != "readonly",
            "kind": kind,
            "runtime": runtime,
            "note": "路径/列表类：仅启动时读取，请改 .env 后重启" if kind == "readonly" else "",
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
        "hint": "当前值 = app_state 覆盖 > 环境变量/.env > 默认值。"
                "改动即时生效并落库（重启保留）；路径/列表类只读，需改 .env 后重启。",
    }
