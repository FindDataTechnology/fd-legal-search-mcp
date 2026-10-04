"""指导案例域：最高法指导案例 1-279 号 + 批次通知（law_db.guide_cases）。

guide_cases_search / guide_cases_read 为 business-mcp 等价迁移面；
实现跟踪：lex-platform-v1 task 1.4（含 provenance 契约全工具覆盖）。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .errors import not_wired


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool
    def guide_cases_search(
        title_query: str,
        doc_type: str | None = None,
        reference_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search guiding-case titles, ranked exact > prefix > contains.

        doc_type / reference_status 为封闭词表过滤（非法值如实拒绝）。
        结果逐条携带 provenance（source=law_db.guide_cases, stable_id=case_id）。
        与 business-mcp guide_cases_search 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("guide_cases_search", "1.4")

    @mcp.tool
    def guide_cases_read(case_id: str) -> dict[str, Any]:
        """Read one guiding case's full structured sections by case_id.

        与 business-mcp guide_cases_read 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("guide_cases_read", "1.4")