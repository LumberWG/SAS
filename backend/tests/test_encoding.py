# -*- coding: utf-8 -*-
"""行情源响应体编码回归测试。

背景（BACKTEST_LOG 记录 #18）：上交所列表接口声明 UTF-8、历史上返回过 GBK。
固定按 GBK 解码会把「华鑫股份」解成「鍗庨懌鑲′唤」，而且**不会抛异常**，
乱码名称就这样流进了自选/信号/推荐/模拟盘。这里锁住「UTF-8 优先、GBK 兜底」。
"""
from app.data.fallback import _decode_json_bytes


def test_utf8_payload_decoded():
    raw = '{"n": "华鑫股份"}'.encode("utf-8")
    assert _decode_json_bytes(raw) == '{"n": "华鑫股份"}'


def test_gbk_payload_decoded_as_fallback():
    raw = '{"n": "华鑫股份"}'.encode("gbk")
    assert _decode_json_bytes(raw) == '{"n": "华鑫股份"}'


def test_utf8_payload_not_mangled_by_gbk():
    """UTF-8 字节若被当 GBK 解，会变成「鍗庨懌鑲′唤」——必须不出现。"""
    text = _decode_json_bytes('{"n": "华鑫股份"}'.encode("utf-8"))
    assert "鍗" not in text
    assert "华鑫股份" in text


def test_ascii_only_payload():
    raw = b'{"code": "600621"}'
    assert _decode_json_bytes(raw) == '{"code": "600621"}'
