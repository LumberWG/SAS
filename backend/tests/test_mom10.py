"""「10 日涨幅前 10 × 四板块」规则的口径测试（不联网、不读数据）。"""
from scripts.mom10_probe import BOARDS, _board_index


def _name(code: str) -> str | None:
    idx = _board_index(code)
    return list(BOARDS)[idx] if idx >= 0 else None


def test_board_index_maps_four_boards():
    """四板块划分：主板含沪深两市，北交所 92 不能误判成沪市，沪 B 不在规则内。"""
    assert _name("600519") == "主板"
    assert _name("601318") == "主板"
    assert _name("000001") == "主板"
    assert _name("002594") == "主板"
    assert _name("300750") == "创业板"
    assert _name("301029") == "创业板"
    assert _name("688111") == "科创板"
    assert _name("920002") == "北交所"
    assert _name("830799") == "北交所"
    assert _name("900001") is None


def test_boards_partition_without_overlap():
    """板块前缀不重叠：同一代码最多落进一个板块。"""
    for code in ("600519", "000001", "300750", "688111", "920002", "430047"):
        hits = [name for name, prefixes in BOARDS.items() if code.startswith(prefixes)]
        assert len(hits) <= 1
