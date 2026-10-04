"""法条域：检索/读取（law_db）+ 语义检索（Qdrant）+ 条级引用解析。

law_search / law_read 为 business-mcp 等价迁移面（签名逐字对齐，task 7.1）；
law_semantic_search / law_reference 为识律新能力（task 1.2）。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .errors import not_wired


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool
    def law_search(title_query: str, category: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Search FindData law titles, ranked exact > prefix > contains.

        法规库全目录检索（law_db.laws，含别名治理）；category 接受同义拼写。
        结果逐条携带 provenance（source=law_db.laws, stable_id=law_id）。
        与 business-mcp law_search 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("law_search", "7.1")

    @mcp.tool
    def law_read(law_id: int) -> dict[str, Any]:
        """Read one law's metadata and text chunks by law_id.

        返回法规详情与条级 chunk 结构，供正文阅读与条级引用使用。
        与 business-mcp law_read 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("law_read", "7.1")

    @mcp.tool
    def law_semantic_search(query: str, top_k: int = 10) -> dict[str, Any]:
        """法条语义检索：Qwen3-Embedding-0.6B 查询向量 -> Qdrant law_chunks_v2。

        按语义相似度返回条级 chunk，逐条携带 provenance
        （source=law_db.law_chunks_v2, stable_id=point id, as_of=导出日期）。
        实现跟踪：lex-platform-v1 task 1.2。
        """
        raise not_wired("law_semantic_search", "1.2")

    @mcp.tool
    def law_reference(citation: str) -> dict[str, Any]:
        """条级引用解析：把「《民法典》第一百四十三条」类引用解析到法规目录条级定位。

        无法解析时如实返回 not_found（含可读原因），绝不猜测。
        实现跟踪：lex-platform-v1 task 1.2。
        """
        raise not_wired("law_reference", "1.2")