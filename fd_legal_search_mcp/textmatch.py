"""标题匹配的共用原语：LIKE 转义与 exact/prefix/contains 排序。

这四个工具面（法条 / 类案 / 指导案例 / 五源语料）都用「标题 ILIKE + 三段排序」
检索。此前每处各写一份逐字相同的 `_escape_like` / `_rank`——四个副本意味着
一处改口径要改四处，而漏改的表现是「某个工具面排序与其他不一致」这种极难
发现的分叉。集中在这里，单一真源。
"""

from __future__ import annotations


def escape_like(q: str) -> str:
    """转义 LIKE/ILIKE 元字符，使用户输入按字面匹配。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def rank(title: str | None, needle: str) -> int:
    """0 exact / 1 prefix / 2 contains — 与 SQL CASE 排序一致。"""
    t = (title or "").lower()
    if t == needle:
        return 0
    if t.startswith(needle):
        return 1
    return 2
