"""第 1 次排序测试：扫描命中按评分排名、同股并列、落库 score/rank。"""
from app.data.cache import Database
from app.screener.scanner import rank_hits


def hit(code: str, score: float | None, strategy: str = "S1", price: float = 10.0) -> dict:
    item = {"code": code, "name": code, "strategy": strategy, "price": price}
    if score is not None:
        item["extra"] = {"score": score}
    return item


class TestRankHits:
    def test_sorted_by_score_desc(self):
        hits = [hit("600001", 2.0), hit("600002", 5.0), hit("600003", -1.0)]
        rank_hits(hits)
        assert [h["code"] for h in hits] == ["600002", "600001", "600003"]
        assert [h["rank"] for h in hits] == [1, 2, 3]

    def test_same_stock_ties_share_rank(self):
        """同一只股票命中多个策略时并列同一名次，前端聚合后名次连续。"""
        hits = [
            hit("600001", 5.0, "A"),
            hit("600001", 4.0, "B"),
            hit("600002", 3.0, "A"),
        ]
        rank_hits(hits)
        assert [h["rank"] for h in hits] == [1, 1, 2]

    def test_missing_score_treated_as_zero(self):
        hits = [hit("600001", None), hit("600002", 1.0)]
        rank_hits(hits)
        assert [h["code"] for h in hits] == ["600002", "600001"]
        assert [h["rank"] for h in hits] == [1, 2]

    def test_empty_list(self):
        assert rank_hits([]) == []


class TestSignalStore:
    def test_score_and_rank_persisted(self, tmp_path):
        db = Database(tmp_path / "test.db")
        db.save_signals(
            [
                {"code": "600001", "name": "甲", "strategy": "A", "price": 10.0,
                 "rank": 1, "extra": {"score": 5.5}},
                {"code": "600002", "name": "乙", "strategy": "A", "price": 20.0,
                 "rank": 2, "score": 3.0},
            ],
            "2026-10-26",
        )
        rows = {r["code"]: r for r in db.list_signals()}
        assert rows["600001"]["score"] == 5.5
        assert rows["600001"]["rank"] == 1
        # score 也可直接从顶层字段取
        assert rows["600002"]["score"] == 3.0
        assert rows["600002"]["rank"] == 2
