"""类案域：裁判文书检索与正文读取（wenshu_db + RustFS zstd 块）。

wenshu_search / wenshu_read 为 business-mcp 等价迁移面（签名逐字对齐）；
实现跟踪：lex-platform-v1 task 1.3 打通（RustFS 读块复用 business-mcp
wenshu_body.py 的 8MB/块、约 500 篇/块、头 JSON {docs, offsets} 约定）。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .errors import not_wired


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool
    def wenshu_search(
        title_query: str,
        doc_type: str | None = None,
        province: str | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search judgment titles (1,050万篇裁判文书), ranked exact > prefix > contains.

        案号内嵌于标题（如「（2024）京01民初1号」），无需单独案号参数。
        结果逐条携带 provenance（source=wenshu_db.judgments, stable_id=doc_id）。
        与 business-mcp wenshu_search 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("wenshu_search", "1.3")

    @mcp.tool
    def wenshu_read(doc_id: str) -> dict[str, Any]:
        """Read one judgment's full text (zstd blocks on RustFS).

        正文按块解压读取；条目存在而正文缺失时如实标注，绝不编造正文。
        与 business-mcp wenshu_read 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("wenshu_read", "1.3")