"""测试全局夹具。

**关掉访问令牌**：`backend/.env` 里一旦配了 `API_TOKEN`，`app.main._token_guard` 会让所有
`/api/*` 在没有 `X-API-Token` 时直接 401，而测试用的 `TestClient` 不带这个头 —— 于是
「接口能返回数据」这一整类用例会以 `401`/`KeyError` 成片失败，看起来像功能坏了。

做法是**就地改单例属性**而不是清 `get_settings` 的 lru 缓存：后者会造出第二个 Settings 对象，
各模块手里还攥着旧引用，行为会分裂（与 `api/settings.py` 的写模型同一条理由）。
要测令牌本身的行为，在用例里用 `monkeypatch.setattr(get_settings(), "api_token", "x")` 临时设回去。
"""
from __future__ import annotations

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True, scope="session")
def _disable_api_token():
    """整个测试会话期间不校验令牌，结束时还原。"""
    settings = get_settings()
    original = getattr(settings, "api_token", "")
    settings.api_token = ""
    try:
        yield
    finally:
        settings.api_token = original


@pytest.fixture(autouse=True)
def _restore_settings_singleton():
    """每个用例结束后还原 Settings 单例的所有字段，杜绝「全量顺序污染」。

    `param_specs.update_settings()` 是**就地改单例**（不能清 lru 缓存，否则模块各持一份
    行为分裂）。此前一个用例写下 `paper_max_positions=1`，后面测「主账本应买 2 只」
    就只买到 1 只；单独跑却全绿。快照/还原只在测试夹具里，不改生产代码。
    """
    settings = get_settings()
    snapshot = {
        name: getattr(settings, name, None)
        for name in type(settings).model_fields
    }
    try:
        yield
    finally:
        for name, value in snapshot.items():
            if getattr(settings, name, None) != value:
                setattr(settings, name, value)
