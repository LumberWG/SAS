"""「成交额前 50 + 四条件」规则的口径测试（不联网、不读数据）。"""
import numpy as np

from scripts.amount50_probe import GIANTS, _normalize_turn


def test_giants_blacklist():
    """巨无霸剔除黑名单：覆盖银行/石油/保险龙头，且不含普通个股。"""
    assert "601398" in GIANTS   # 工行
    assert "601857" in GIANTS   # 中石油
    assert "601318" in GIANTS   # 平安保险
    assert "600519" not in GIANTS  # 茅台（高成交额但非巨无霸波动，文章要的是剔除权重股）


def test_normalize_turn_unifies_scale():
    """换手率量纲归一：百分比存储(>1)转比例，比例(<1)与 NaN 保持不变。"""
    arr = np.array([0.57, 6.3, np.nan, 12.0, 0.02])
    out = _normalize_turn(arr)
    assert np.isclose(out[0], 0.57)      # 已是比例
    assert np.isclose(out[1], 0.063)     # 6.3% -> 0.063
    assert np.isnan(out[2])
    assert np.isclose(out[3], 0.12)      # 12% -> 0.12
    assert np.isclose(out[4], 0.02)
