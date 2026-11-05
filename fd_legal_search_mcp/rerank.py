"""law_semantic_search 的可解释重排（纯函数，零新依赖）。

Qdrant cosine 相似度提供召回，但纯向量序对两类查询有系统性偏差：
精确标题查询（向量语义接近但用户要的是那部法）与总则/附则 chunk（语义泛、
常抢前排）。本层在扩窗召回之上做线性特征加权，权重全部可解释、进仓可调：

- title_cover   标题对查询词的覆盖（exact=1.0 / prefix=0.6 / contains=0.3 / 0）
- law_status    现行有效 +0.15，已失效/废止 -0.3（效力状态是法条检索硬信号）
- position      chunk_no 靠前先验（总则条先于附则）：1/(1+chunk_no/50) × 0.1

相似度分数始终参与（score 归一化后为主权重），特征只做重排不替换。
回归门（金集）约束任何权重调整：exact 段 hit@5 不降。
"""
from __future__ import annotations

from typing import Any

from .textmatch import rank as title_rank

# 权重（进仓可调；调整必须附金集对照，回归门拦 exact 段回退）
W_SCORE = 1.0        # Qdrant 相似度（cosine，[-1,1]，归一化到 [0,1]）
W_TITLE_COVER = 0.5
W_STATUS = 0.15
W_POSITION = 0.1

_STATUS_BONUS: dict[str, float] = {
    "有效": 0.15,
    "现行有效": 0.15,
    "已失效": -0.3,
    "废止": -0.3,
    "失效": -0.3,
}

RERANK_MULTIPLIER = 4  # 召回扩窗倍数
RERANK_WINDOW_MAX = 50  # 与 limits.MAX_TOP_K 对齐（top_k 封顶时召回窗不缩）


def title_cover(title: str | None, query: str) -> float:
    """标题对查询的覆盖度：exact 1.0 / prefix 0.6 / contains 0.3 / 无 0。"""
    r = title_rank(title, query.lower())
    return {0: 1.0, 1: 0.6}.get(r, 0.3 if query and query in (title or "").lower() else 0.0)


def rerank_score(point: dict[str, Any], query: str, max_score: float) -> float:
    """单点重排分 = 相似度主权重 + 特征项。point 为工具返回的 result 行。"""
    score = float(point.get("score") or 0.0)
    norm = (score + 1.0) / 2.0 if score <= 1.0 else score  # cosine [-1,1] → [0,1]
    total = W_SCORE * norm
    total += W_TITLE_COVER * title_cover(point.get("title"), query)
    total += W_STATUS * _STATUS_BONUS.get(str(point.get("law_status") or ""), 0.0)
    chunk_no = int(point.get("chunk_no") or 0)
    total += W_POSITION * (1.0 / (1.0 + chunk_no / 50.0))
    return total


def rerank(results: list[dict[str, Any]], query: str, top_k: int) -> list[dict[str, Any]]:
    """对扩窗召回重排并截回 top_k；逐条附 reranked=true（溯源不动）。"""
    if not results:
        return []
    ranked = sorted(
        results,
        key=lambda r: rerank_score(r, query, 1.0),
        reverse=True,
    )[:top_k]
    for r in ranked:
        r["reranked"] = True
    return ranked


def recall_window(top_k: int) -> int:
    """召回扩窗数：top_k × RERANK_MULTIPLIER，但永不小于 top_k、封顶与
    limits.MAX_TOP_K 同级——top_k 打满时召回窗不缩。"""
    from . import limits

    return min(max(top_k * RERANK_MULTIPLIER, top_k), max(limits.MAX_TOP_K, top_k))
