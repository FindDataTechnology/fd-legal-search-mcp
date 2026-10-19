"""指导案例域：最高法指导案例 1-279 号 + 批次通知（law_db.guide_cases）。

guide_cases_search / guide_cases_read 为 business-mcp 等价迁移面；
实现跟踪：lex-platform-v1 task 1.4（含 provenance 契约全工具覆盖）。

移植自 fd-find-data-business-mcp tools_guide_cases.py：exact>prefix>contains
排序、doc_type/reference_status 封闭词表、分节全文读取。差异（本仓语义）：
- 非法过滤值 / 未命中 case_id 走 errors 模块（Validation / NotFound 工具错误），
  不再返回 {"status": ...} 字典；
- 爬虫记账列（content_hash/fetched_at/loaded_at）从不进入响应；
- 每条结果经 with_provenance/ensure_provenance 携带三件套
  （source=law_db.guide_cases，stable_id=source_id，as_of=发布日期）。
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .db import fetch_all, get_pool
from .errors import NotFound, Validation
from .provenance import Provenance, ensure_provenance, with_provenance
from .textmatch import escape_like

#: provenance.source 固定口径（spec lex-mcp-surface）。
PROVENANCE_SOURCE = "law_db.guide_cases"

_MAX_LIMIT = 200

_DOC_TYPES = frozenset({"case", "notice"})

_REFERENCE_STATUSES = frozenset({"有效", "不再参照"})

#: 案例正文的六个已发布分节。语料中从未发布的分节为 NULL，
#: 读取响应里整个省略该键（不留 null 占位）。
_SECTION_KEYS = ("keyword", "gist", "facts", "result", "reasoning", "statutes")

_SEARCH_FIELDS = (
    "source_id", "case_no", "doc_type", "title",
    "listed_at", "publish_at", "source", "reference_status",
)

_READ_FIELDS = (
    "source_id", "case_no", "doc_type", "title",
    "listed_at", "publish_at", "source", "source_url",
)


def _reject_filter(field: str, value: str, valid: frozenset[str]) -> Validation:
    """封闭词表外的过滤值如实拒绝（validation 错误语义），绝不猜测近似词。"""
    return Validation(f"validation: {field}={value!r} 不在封闭词表内，仅接受 {sorted(valid)}")


def _as_of(row: dict[str, Any]) -> str:
    """数据口径日期：发布日期（ISO），缺失时回退列入日期。

    guide_cases 两列均为非空实测（publish_at/listed_at 0 个 NULL）；
    若两者皆缺失返回空串，由 ensure_provenance 作为 bug 拦下，不降级。
    """
    return str(row.get("publish_at") or row.get("listed_at") or "")


def _stable_id(row: dict[str, Any]) -> str:
    return str(row.get("source_id") or "")


def register(mcp: FastMCP, settings: Settings) -> None:

    def _pool() -> Any:
        return get_pool("FDLEX_LAW_DATABASE_URL", settings.law_database_url)

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
        q = (title_query or "").strip()
        if not q:
            return {"results": [], "count": 0, "truncated": False}
        if doc_type:
            doc_type = str(doc_type).strip()
            if doc_type not in _DOC_TYPES:
                raise _reject_filter("doc_type", doc_type, _DOC_TYPES)
        if reference_status:
            reference_status = str(reference_status).strip()
            if reference_status not in _REFERENCE_STATUSES:
                raise _reject_filter("reference_status", reference_status, _REFERENCE_STATUSES)
        limit = min(max(int(limit), 1), _MAX_LIMIT)
        esc = escape_like(q)
        sql = (
            "SELECT source_id, case_no, doc_type, title,\n"
            "       listed_at::date::text AS listed_at,\n"
            "       publish_at::date::text AS publish_at,\n"
            "       source, reference_status,\n"
            "       CASE WHEN title ILIKE %s THEN 0\n"
            "            WHEN title ILIKE %s THEN 1\n"
            "            ELSE 2 END AS rank\n"
            "FROM guide_cases\n"
            "WHERE title ILIKE %s\n"
        )
        params: list[Any] = [esc, esc + "%", f"%{esc}%"]
        if doc_type:
            sql += "  AND doc_type = %s\n"
            params.append(doc_type)
        if reference_status:
            sql += "  AND reference_status = %s\n"
            params.append(reference_status)
        sql += "ORDER BY rank, title\nLIMIT %s"
        params.append(limit + 1)
        rows = fetch_all(_pool(), sql, tuple(params))
        truncated = len(rows) > limit
        items = [
            with_provenance(
                {k: row.get(k) for k in _SEARCH_FIELDS},
                Provenance(
                    source=PROVENANCE_SOURCE,
                    stable_id=_stable_id(row),
                    as_of=_as_of(row),
                ),
            )
            for row in rows[:limit]
        ]
        ensure_provenance(items)
        return {"results": items, "count": len(items), "truncated": truncated}

    @mcp.tool
    def guide_cases_read(case_id: str) -> dict[str, Any]:
        """Read one guiding case's full structured sections by case_id.

        与 business-mcp guide_cases_read 等价迁移（lex-platform-v1 task 7.1）。
        """
        rows = fetch_all(
            _pool(),
            "SELECT source_id, case_no, doc_type, title,\n"
            "       listed_at::date::text AS listed_at,\n"
            "       publish_at::date::text AS publish_at,\n"
            "       source, source_url,\n"
            "       keyword, gist, facts, result, reasoning, statutes,\n"
            "       body, reference_status, reference_note\n"
            "FROM guide_cases\n"
            "WHERE source_id = %s",
            (str(case_id),),
        )
        if not rows:
            raise NotFound(f"not_found: guide_cases 中不存在 case_id={case_id!r}")
        row = rows[0]
        out: dict[str, Any] = {k: row.get(k) for k in _READ_FIELDS}
        for key in _SECTION_KEYS:
            if row.get(key):
                out[key] = row[key]
        out["body"] = row.get("body")
        out["reference_status"] = row.get("reference_status")
        out["reference_note"] = row.get("reference_note")
        out = with_provenance(
            out,
            Provenance(source=PROVENANCE_SOURCE, stable_id=_stable_id(row), as_of=_as_of(row)),
        )
        ensure_provenance([out])
        return out
