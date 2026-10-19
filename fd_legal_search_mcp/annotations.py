"""工具行为属性（mcp-tool-annotations）。

**本服务 10 个工具全部只读**：检索、读取、条级引用解析都只查询既有语料，
不写任何持久状态、不落缓存、不改索引。故 `readOnlyHint` 全为真。

`openWorldHint` 全部为假——这里值得说清判据，否则很容易照着"调了外部服务
就叫开放世界"把它误标成真：

- 开放世界（True）指结果取决于一个**不可枚举、会变化**的外部实体集合，
  典型是 LLM（同一 prompt 明天可能给出不同答案）与网页搜索。
- 本服务的数据源是**封闭语料**：law_db 的法规/案例/语料三张表、Qdrant 的
  law_chunks_v2 索引、RustFS 的文书分片。集合是已知且版本化的，同一个
  stable_id 在同一个 as_of 上恒得同一内容。嵌入模型只用来把查询串变成向量，
  检索结果仍来自那批固定 point。

所以"需要连数据库"不等于开放世界；`openWorldHint=False` 表示调用方可以假定
结果在语料版本不变时是稳定可复现的。`idempotentHint` 全为真：同参数重复调用
不改变任何状态，返回同一结果。

注意这里**没有**声明 `destructiveHint=True` 的可能——本服务没有删除/改写接口。
"""
from __future__ import annotations

from typing import Any

#: 只读 + 幂等 + 封闭语料。
READ_ONLY: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: 工具名 → 属性。10 个工具用同一份（内容相同仍逐名登记：这样新增工具漏登记
#: 会在 selfcheck 里报出来，而不是靠"默认值恰好对"）。
TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    name: dict(READ_ONLY)
    for name in (
        "law_search",
        "law_read",
        "law_semantic_search",
        "law_reference",
        "wenshu_search",
        "wenshu_read",
        "guide_cases_search",
        "guide_cases_read",
        "legal_docs_search",
        "legal_docs_read",
    )
}


def annotations_for(name: str) -> dict[str, Any]:
    """取属性副本；未登记即报错（沉默的默认值会让漏登记的工具蒙混过关）。"""
    try:
        return dict(TOOL_ANNOTATIONS[name])
    except KeyError:
        raise KeyError(
            f"工具 {name!r} 未在 annotations.TOOL_ANNOTATIONS 登记行为属性"
        ) from None
