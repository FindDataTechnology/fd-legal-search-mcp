"""五源法律语料域（law_db.legal_documents，唯一键 (source, source_id)）。

五源：gov-rules / mfa-treaty / ccdi-supervisory / rmfyalk / party-regulation。
legal_docs_search / legal_docs_read 为 business-mcp 等价迁移面；
实现跟踪：lex-platform-v1 task 7.1。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .errors import not_wired


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool
    def legal_docs_search(
        title_query: str,
        source: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search legal-document titles across the five corpora (47,256 篇).

        source 为五源代码封闭词表，非法值如实拒绝。
        结果逐条携带 provenance（source=law_db.legal_documents, stable_id=source_id）。
        与 business-mcp legal_docs_search 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("legal_docs_search", "7.1")

    @mcp.tool
    def legal_docs_read(doc_id: int) -> dict[str, Any]:
        """Read one legal document by doc_id.

        与 business-mcp legal_docs_read 等价迁移（lex-platform-v1 task 7.1）。
        """
        raise not_wired("legal_docs_read", "7.1")