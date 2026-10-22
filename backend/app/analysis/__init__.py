"""分析引擎：技术面评分与智能建议。

注意：此处只导出类，不导出 recommender 单例——
避免单例名与子模块名 recommender 相同造成命名遮蔽
（会影响 monkeypatch 等按路径引用的场景）。
"""

from app.analysis.recommender import Recommendation, Recommender
from app.analysis.scoring import ScoreResult, score_technical

__all__ = [
    "Recommendation",
    "Recommender",
    "ScoreResult",
    "score_technical",
]
