"""并行账本判读口径（P3）的纯函数测试。"""

from app.paper.ledger import LEDGER_ROLE, _max_drawdown_pct, ledger_interpretation


def test_max_drawdown_basic():
    equity = [
        {"date": "2026-11-01", "total_asset": 100.0},
        {"date": "2026-11-02", "total_asset": 120.0},
        {"date": "2026-11-03", "total_asset": 90.0},
        {"date": "2026-11-04", "total_asset": 110.0},
    ]
    # 峰值 120，谷 90 → 回撤 (120-90)/120 = 25%
    assert _max_drawdown_pct(equity) == 25.0


def test_max_drawdown_monotonic_up_is_zero():
    equity = [
        {"date": "2026-11-01", "total_asset": 100.0},
        {"date": "2026-11-02", "total_asset": 110.0},
        {"date": "2026-11-03", "total_asset": 130.0},
    ]
    assert _max_drawdown_pct(equity) == 0.0


def test_max_drawdown_empty():
    assert _max_drawdown_pct([]) == 0.0


def test_interpretation_shape():
    info = ledger_interpretation()
    assert info["role"] == LEDGER_ROLE
    assert "参数敏感性" in info["note"]
    # 强调非独立策略、看最大回撤、主曲线是 paper_equity
    assert "相互独立的策略" in info["note"]
    assert "最大回撤" in info["note"]
    assert "paper_equity" in info["note"]
