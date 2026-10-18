"""五源法律语料域（law_db.legal_documents，唯一键 (source, source_id)）。

五源：gov-rules / mfa-treaty / ccdi-supervisory / rmfyalk / party-regulation。
`legal_docs_search` / `legal_docs_read` 与 business-mcp 的同名工具**等价**：
查询语义（exact > prefix > contains 排序）、来源封闭词表、字段白名单、
"正文未抓取时如实说明"的返回形状都逐项对齐，避免两套口径分叉。

与 business-mcp 的差别只在**落点**：那边经联邦查询层（`domain.query`），
这边直连本服务已配置的 `FDLEX_LAW_DATABASE_URL`；以及本服务所有结果统一
携带 provenance 三件套（source / stable_id / as_of），这是 lex 侧契约。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .db import fetch_all, get_pool
from .provenance import Provenance, ensure_provenance, with_provenance

_LAW_DB_ENV = "FDLEX_LAW_DATABASE_URL"

_BRAND = "finddata"

_MAX_LIMIT = 200

#: 五源封闭词表，与 legal_documents.source 存储值逐一对应。
#: 非法值直接拒绝（`invalid_filter`），不静默匹配空集。
_SOURCES: tuple[str, ...] = (
    "ccdi-supervisory-regulations",
    "gov-rules-library",
    "mfa-treaty-library",
    "party-regulation-library",
    "rmfyalk-case-library",
)

_SOURCE_LABELS_ZH: dict[str, str] = {
    "ccdi-supervisory-regulations": "纪检监察法规库",
    "gov-rules-library": "政府规章库",
    "mfa-treaty-library": "外交部条约库",
    "party-regulation-library": "党内法规库",
    "rmfyalk-case-library": "人民法院案例库",
}

#: 检索命中只带这些字段：不带正文、不带爬虫溯源列、不带 metadata jsonb。
_SEARCH_FIELDS = (
    "id", "title", "source", "doc_type", "status", "authority",
    "language", "published_at", "effective_at", "has_content",
)

#: 单篇读取额外暴露 source_url 与 metadata（去掉爬虫内部 object_keys）。
_READ_FIELDS = (
    "id", "title", "source", "doc_type", "status", "authority",
    "language", "published_at", "effective_at", "source_url", "metadata",
)

_NO_BODY_NOTE = (
    "该文档正文未抓取入库：源站正文为页面异步渲染或附件体积超限所致，"
    "现仅提供元数据。"
)

_SOURCE_TABLE = "law_db.legal_documents"


def _escape_like(q: str) -> str:
    """转义 LIKE 元字符，使用户输入按字面匹配。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rank(title: str | None, needle: str) -> int:
    """0 exact / 1 prefix / 2 contains — 与 SQL CASE 排序一致。"""
    t = (title or "").lower()
    if t == needle:
        return 0
    if t.startswith(needle):
        return 1
    return 2


def _strip_object_keys(metadata: Any) -> Any:
    """去掉爬虫内部的 object_keys 条目。"""
    if isinstance(metadata, dict):
        return {k: v for k, v in metadata.items() if k != "object_keys"}
    return metadata


def _provenance(row: dict[str, Any]) -> Provenance:
    """as_of 用入库时间（loaded_at）——它是这行数据真实成立的口径日期。"""
    as_of = row.get("loaded_at")
    if hasattr(as_of, "isoformat"):
        as_of = as_of.isoformat()
    return Provenance(
        source=_SOURCE_TABLE,
        stable_id=str(row.get("id")),
        as_of=str(as_of) if as_of else "",
    )


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool
    def legal_docs_search(
        title_query: str,
        source: str | None = None,
        doc_type: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search legal-document titles across the five corpora (47,256 篇).

        Ranked exact > prefix > contains, case-insensitive. `source` is a closed
        vocabulary of the five corpus codes — an unknown value is rejected with
        `invalid_filter` plus the valid list, never silently matched to nothing.
        `doc_type` is an equality filter. At most 200 rows.

        Document bodies are never returned here; `has_content` says whether
        `legal_docs_read` can serve one. Every hit carries provenance
        (source=law_db.legal_documents, stable_id=doc_id, as_of=入库时间).
        """
        q = (title_query or "").strip()
        if not q:
            return {"results": [], "count": 0, "truncated": False, "brand": _BRAND}
        src = (source or "").strip() or None
        if src is not None and src not in _SOURCES:
            return {
                "status": "invalid_filter",
                "field": "source",
                "valid_sources": list(_SOURCES),
                "brand": _BRAND,
            }
        limit = min(max(int(limit), 1), _MAX_LIMIT)
        esc = _escape_like(q)
        params: list[Any] = [f"%{esc}%"]
        sql = (
            "SELECT id, title, source, doc_type, status, authority, language,\n"
            "       published_at::date::text AS published_at,\n"
            "       effective_at::date::text AS effective_at,\n"
            "       (content IS NOT NULL) AS has_content,\n"
            "       loaded_at\n"
            "FROM legal_documents\n"
            "WHERE title ILIKE %s\n"
        )
        if src is not None:
            params.append(src)
            sql += "  AND source = %s\n"
        dtype = (doc_type or "").strip() or None
        if dtype is not None:
            params.append(dtype)
            sql += "  AND doc_type = %s\n"
        params.append(limit + 1)
        sql += "ORDER BY title\nLIMIT %s"

        pool = get_pool(_LAW_DB_ENV, settings.law_database_url)
        rows = fetch_all(pool, sql, tuple(params))

        needle = q.lower()
        rows.sort(key=lambda r: (_rank(r.get("title"), needle), r.get("title") or ""))
        truncated = len(rows) > limit
        hits = []
        for row in rows[:limit]:
            item = {k: row.get(k) for k in _SEARCH_FIELDS}
            item["has_content"] = bool(row.get("has_content"))
            item["source_label_zh"] = _SOURCE_LABELS_ZH.get(row.get("source") or "")
            item["brand"] = _BRAND
            hits.append(with_provenance(item, _provenance(row)))
        return {
            "results": ensure_provenance(hits),
            "count": len(hits),
            "truncated": truncated,
            "brand": _BRAND,
        }

    @mcp.tool
    def legal_docs_read(doc_id: int) -> dict[str, Any]:
        """Read one legal document by `doc_id`, body included only when fetched.

        Returns full metadata (including `source_url` and the crawler `metadata`
        jsonb minus its internal `object_keys`) plus `content_available`. When
        the body never made it into the corpus the answer carries an honest
        `note` and the `content` key is **absent** — not an empty string.
        Unknown ids return `not_found`.
        """
        pool = get_pool(_LAW_DB_ENV, settings.law_database_url)
        rows = fetch_all(
            pool,
            "SELECT id, title, source, doc_type, status, authority, language,\n"
            "       published_at::date::text AS published_at,\n"
            "       effective_at::date::text AS effective_at,\n"
            "       source_url, metadata, loaded_at,\n"
            "       (content IS NOT NULL) AS content_available, content\n"
            "FROM legal_documents\n"
            "WHERE id = %s",
            (int(doc_id),),
        )
        if not rows:
            return {"status": "not_found", "brand": _BRAND}
        row = rows[0]
        out: dict[str, Any] = {k: row.get(k) for k in _READ_FIELDS}
        out["source_label_zh"] = _SOURCE_LABELS_ZH.get(row.get("source") or "")
        out["metadata"] = _strip_object_keys(out.get("metadata"))
        out["content_available"] = bool(row.get("content_available"))
        if out["content_available"]:
            out["content"] = row.get("content")
        else:
            out["note"] = _NO_BODY_NOTE
        out["brand"] = _BRAND
        return with_provenance(out, _provenance(row))
