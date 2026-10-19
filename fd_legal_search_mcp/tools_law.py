"""法条域：检索/读取（law_db）+ 语义检索（Qdrant）+ 条级引用解析。

law_search / law_read 为 business-mcp 等价迁移面（签名逐字对齐，task 7.1）；
law_semantic_search / law_reference 为识律新能力（task 1.2）。

数据坐标：law_db.laws（法规目录）、law_db.law_chunks_v2（条级 chunk，
char_start/char_end 与 laws.content 对齐、按 chunk_no 连续覆盖全文）、
Qdrant law_chunks_v2（1024 维 cosine，payload.law_id 为字符串 keyword）。
一切未命中如实返回（空结果或 not_found），绝不编造数据。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastmcp import FastMCP

from .config import Settings
from .db import fetch_all, fetch_one, get_pool, get_qdrant
from .embedding import get_embedder
from .errors import NotFound, Validation
from .provenance import Provenance, ensure_provenance, with_provenance
from .textmatch import escape_like, rank
from .annotations import annotations_for
from . import limits

_LAW_DB_ENV = "FDLEX_LAW_DATABASE_URL"

_BRAND = "finddata"

# 上限的唯一真源在 limits.py（mcp-result-contract）
_MAX_LIMIT = limits.MAX_LIST_ITEMS
_MAX_TOP_K = limits.MAX_TOP_K

# 溯源口径：law_db 各表 created_at（入库时间）为每行真实成立的口径日期；
# 表内无独立"发布时间戳"字段时绝不编造（publish 是法规的发布日期，不是数据口径）。
_SOURCE_LAWS = "law_db.laws"
_SOURCE_CHUNKS = "law_db.law_chunks_v2"

_METADATA_FIELDS = ("id", "title", "category", "type", "status", "publish", "expiry")

# The crawled corpus stores crawler-batch labels (重下载/新法速递) and synonym
# spellings (地方法规/地方性法规) in the same category column. Customers see
# only the canonical vocabulary; batch labels never win a duplicate group and
# are not filterable (law-corpus-hygiene design D2/D3).
_BATCH_CATEGORIES = frozenset({"重下载", "新法速递"})
_CATEGORY_SYNONYMS = {"地方法规": "地方性法规"}
_FILTER_EXPANSION: dict[str, set[str]] = {}
for _syn, _canon in _CATEGORY_SYNONYMS.items():
    _FILTER_EXPANSION.setdefault(_canon, {_canon}).add(_syn)

# ---------------------------------------------------------------- 解析原语

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}
_FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_CN_NUMERAL_RUN = "零一二两三四五六七八九十百千万"

# 条号（可带「之N」款序）：第[数字/中文数字]条(之[...])? —— 数字连跑到「条」为止，
# 因此「第十条一切单位和个人」不会吞进正文首字。
_ARTICLE_RE = re.compile(
    r"第([0-9０-９" + _CN_NUMERAL_RUN + r"]+)条(?:之([0-9０-９" + _CN_NUMERAL_RUN + r"]+))?"
)
_BOOK_TITLE_RE = re.compile(r"《(.+?)》")


def _cn_to_int(s: str) -> int:
    """中文数字 -> int（支持 十/百/千/万：二十=20、一百四十三=143、一千零一=1001）。"""
    total = section = current = 0
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit == 10000:
                section = (section + current) * unit
                total += section
                section = current = 0
            else:
                section += (current or 1) * unit
                current = 0
    return total + section + current


def _num_to_int(numeral: str) -> int:
    """条号数字串 -> int：全角转半角；纯数字直接解析，否则按中文数字解析。"""
    s = numeral.translate(_FW_DIGITS)
    if s.isdigit():
        return int(s)
    return _cn_to_int(s)


def _parse_citation(citation: str) -> tuple[str | None, int | None, str | None, str | None]:
    """把「《民法典》第一百四十三条」拆成 (标题, 条号, 之N原文, 条号原文)。

    标题优先取书名号内容；无书名号时取条号之前的文本（去尾部标点）。
    解析失败的部分如实返回 None，绝不猜。
    """
    text = (citation or "").strip()
    m = _BOOK_TITLE_RE.search(text)
    art = _ARTICLE_RE.search(text)
    if m:
        title = m.group(1).strip()
    elif art:
        title = text[: art.start()].strip().strip("，。；、：:,;　 ") or None
    else:
        title = text.strip().strip("，。；、：:,;　 ") or None
    if art is None:
        return title, None, None, None
    suffix_raw = f"之{art.group(2)}" if art.group(2) else None
    return title, _num_to_int(art.group(1)), suffix_raw, art.group(1)


# ---------------------------------------------------------------- 行级排序（business-mcp 权威性择优）

def _normalize_category(category: str | None) -> str | None:
    """Map a synonym spelling to its canonical category (else pass through)."""
    if not category:
        return category
    return _CATEGORY_SYNONYMS.get(category, category)


def _category_class(row: dict[str, Any]) -> int:
    """0 = real-law category, 1 = crawler-batch label."""
    return 1 if (row.get("category") or "") in _BATCH_CATEGORIES else 0


def _status_class(row: dict[str, Any]) -> int:
    """0 = currently effective, 1 = anything else (incl. crawler states)."""
    return 0 if (row.get("status") or "") == "有效" else 1


_DATE_PREFIX_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _publish_num(row: dict[str, Any]) -> int:
    """publish(varchar, 形如 '2024-06-06 00:00:00') -> YYYYMMDD int；解析不了为 0。"""
    m = _DATE_PREFIX_RE.match(str(row.get("publish") or ""))
    return int(m.group(1) + m.group(2) + m.group(3)) if m else 0


def _authority_key(row: dict[str, Any]) -> tuple[int, int, int, int]:
    """重复标题的权威性择优：真实类别 > 爬虫批次，有效 > 其他，最新发布优先，NULL 最后。"""
    return (_category_class(row), _status_class(row),
            1 if row.get("publish") is None else 0, -_publish_num(row))


def _dedupe_authoritative(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per exact title (authoritative pick). Safety net over the
    SQL DISTINCT ON — idempotent when the database already did the work."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r.get("title") or "").strip().lower(), []).append(r)
    return [min(group, key=_authority_key) for group in groups.values()]


def _resolve_law(pool: Any, title: str) -> dict[str, Any]:
    """标题 -> laws 唯一权威行：exact > 全称(中华人民共和国+简称) > 前缀 > contains，
    同级按权威性择优。找不到抛 NotFound（含可读原因），绝不猜。"""
    esc = escape_like(title)
    rows = fetch_all(
        pool,
        "SELECT id, title, category, type, status, publish, created_at\n"
        "FROM laws WHERE title ILIKE %(pattern)s",
        {"pattern": f"%{esc}%"},
    )
    if not rows:
        raise NotFound(
            f"not_found：法规目录中找不到标题含「{title}」的法规（law_db.laws 未命中，不猜测）"
        )
    needle = title.lower()
    full = f"中华人民共和国{needle}"

    def _ref_rank(row: dict[str, Any]) -> tuple[int, ...]:
        t = (row.get("title") or "").lower()
        if t == needle:
            rank = 0
        elif t == full:
            rank = 1  # 「刑法」->「中华人民共和国刑法」这一惯用全称
        elif t.startswith(needle) or t.startswith(full):
            rank = 2
        else:
            rank = 3
        return (rank, *_authority_key(row))

    return min(rows, key=_ref_rank)


# ---------------------------------------------------------------- 结果装配与溯源

def _as_of_date(created_at: Any) -> str:
    """行级 as_of：取入库时间 created_at 的日期部分（law_db 中两表均无 NULL）。"""
    if isinstance(created_at, datetime):
        return created_at.date().isoformat()
    if isinstance(created_at, str) and len(created_at) >= 10:
        return created_at[:10]
    return ""  # 缺口径即 bug，由 ensure_provenance 显式暴露


def _iso(value: Any) -> Any:
    """把可能为 datetime 的字段归一为 ISO 字符串（其余原样）。

    Qdrant 会把 payload 里的日期读成 UTC datetime；原值是无时区钟面时间
    （如 '2025-02-05 00:00:00'），去掉 tzinfo 原样回显，不做时区换算。
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    return value


def _law_item(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: row.get(k) for k in _METADATA_FIELDS}
    out["category"] = _normalize_category(out.get("category"))
    out["brand"] = _BRAND
    return with_provenance(
        out, Provenance(_SOURCE_LAWS, str(row.get("id")), _as_of_date(row.get("created_at")))
    )


def _chunk_item(row: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "chunk_id": row.get("chunk_id"),
        "chunk_no": row.get("chunk_no"),
        "chunk_text": row.get("chunk_text"),
        "char_start": row.get("char_start"),
        "char_end": row.get("char_end"),
    }
    if extra:
        out.update(extra)
    return with_provenance(
        out, Provenance(_SOURCE_CHUNKS, str(row.get("chunk_id")), _as_of_date(row.get("created_at")))
    )


# ---------------------------------------------------------------- SQL

_SEARCH_SQL_HEAD = (
    "SELECT id, title, category, type, status, publish, expiry, created_at\n"
    "FROM (\n"
    "  SELECT DISTINCT ON (tkey)\n"
    "         id, title, category, type, status, publish, expiry, created_at, tkey, rank\n"
    "  FROM (\n"
    "    SELECT id, title, category, type, status, publish, expiry, created_at,\n"
    "           lower(title) AS tkey,\n"
    "           CASE WHEN title ILIKE %(exact)s THEN 0\n"
    "                WHEN title ILIKE %(prefix)s THEN 1\n"
    "                ELSE 2 END AS rank,\n"
    "           CASE WHEN category = ANY(%(batch)s) THEN 1 ELSE 0 END AS cclass,\n"
    "           CASE WHEN status = '有效' THEN 0 ELSE 1 END AS sclass\n"
    "    FROM laws\n"
    "    WHERE title ILIKE %(pattern)s\n"
)
_SEARCH_SQL_TAIL = (
    "  ) cand\n"
    "  ORDER BY tkey, rank, cclass, sclass, publish DESC NULLS LAST\n"
    ") best\n"
    "ORDER BY rank, title\n"
    "LIMIT %(lim)s"
)

_LAW_BY_ID_SQL = (
    "SELECT id, title, category, type, status, publish, expiry, created_at\n"
    "FROM laws WHERE id = %(law_id)s"
)

_CHUNKS_OF_LAW_SQL = (
    "SELECT chunk_id, chunk_no, chunk_text, char_start, char_end, created_at\n"
    "FROM law_chunks_v2 WHERE law_id = %(law_id)s ORDER BY chunk_no"
)

#: 条文窗口查询：按 chunk_no 排序后切片，保证 offset 分页稳定且可分页还原。
_CHUNKS_OF_LAW_WINDOW_SQL = (
    "SELECT chunk_id, chunk_no, chunk_text, char_start, char_end, created_at\n"
    "FROM law_chunks_v2 WHERE law_id = %(law_id)s\n"
    "ORDER BY chunk_no OFFSET %(offset)s LIMIT %(limit)s"
)

_COUNT_CHUNKS_OF_LAW_SQL = (
    "SELECT count(*) AS n FROM law_chunks_v2 WHERE law_id = %(law_id)s"
)

#: 单次返回的条文窗口：默认 200 条（一部典型法规的规模量级），硬上限 500。
#: 整部法规全量回传会让大法典独占调用方上下文，故与正文一样按窗口给。
_DEFAULT_CHUNK_WINDOW = limits.DEFAULT_CHUNK_WINDOW
_MAX_CHUNK_WINDOW = limits.MAX_CHUNK_WINDOW

_CHUNK_CREATED_AT_SQL = (
    "SELECT chunk_id, created_at FROM law_chunks_v2 WHERE chunk_id = ANY(%(ids)s)"
)

_CHUNK_SNAPSHOT_SQL = "SELECT max(created_at) AS m FROM law_chunks_v2"

# 条级摘录上限：单条条文超过此长度时截断并如实标注 truncated。
_EXCERPT_MAX_CHARS = limits.MAX_EXCERPT_CHARS


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool(annotations=annotations_for("law_search"))
    def law_search(title_query: str, category: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Search FindData law titles, ranked exact > prefix > contains.

        法规库全目录检索（law_db.laws，含别名治理）；category 接受同义拼写。
        结果逐条携带 provenance（source=law_db.laws, stable_id=law_id）。
        与 business-mcp law_search 等价迁移（lex-platform-v1 task 7.1）。
        """
        q = (title_query or "").strip()
        if not q:
            return {"results": [], "count": 0, "truncated": False}
        limit = min(max(limit, 1), _MAX_LIMIT)
        esc = escape_like(q)
        params: dict[str, Any] = {
            "pattern": f"%{esc}%",
            "exact": esc,
            "prefix": f"{esc}%",
            "lim": limit + 1,
            "batch": sorted(_BATCH_CATEGORIES),
        }
        sql = _SEARCH_SQL_HEAD
        if category:
            canon = _normalize_category(category.strip())
            if canon in _BATCH_CATEGORIES:
                # Batch labels are crawler bookkeeping, not a customer category.
                return {"results": [], "count": 0, "truncated": False}
            params["categories"] = sorted(_FILTER_EXPANSION.get(canon, {canon}))
            sql += "    AND category = ANY(%(categories)s)\n"
        sql += _SEARCH_SQL_TAIL
        pool = get_pool(_LAW_DB_ENV, settings.law_database_url)
        rows = _dedupe_authoritative(fetch_all(pool, sql, params))
        needle = q.lower()
        rows = sorted(rows, key=lambda r: (rank(r.get("title"), needle), r.get("title") or ""))
        truncated = len(rows) > limit
        results = [_law_item(r) for r in rows[:limit]]
        ensure_provenance(results)
        return {"results": results, "count": min(len(rows), limit), "truncated": truncated}

    @mcp.tool(annotations=annotations_for("law_read"))
    def law_read(law_id: int, offset: int = 0, limit: int = _DEFAULT_CHUNK_WINDOW) -> dict[str, Any]:
        """Read one law's metadata and text chunks by law_id.

        返回法规详情与条级 chunk 结构，供正文阅读与条级引用使用。
        与 business-mcp law_read 等价迁移（lex-platform-v1 task 7.1）。

        条文按窗口返回：单次最多 ``limit`` 条（默认 200，上限 500），从
        ``offset`` 起算。整部法规一次全量回传会让大法典独占调用方上下文；
        ``total_chunks`` 与 ``next_offset`` 给出续读位置，分段拼接即全文。
        """
        pool = get_pool(_LAW_DB_ENV, settings.law_database_url)
        row = fetch_one(pool, _LAW_BY_ID_SQL, {"law_id": law_id})
        if not row:
            raise NotFound(f"not_found：law_id={law_id} 在法规目录（law_db.laws）中不存在")
        law = _law_item(row)
        window = limits.clamp(limit, low=1, high=_MAX_CHUNK_WINDOW)
        start_at = max(0, int(offset))
        # Count first, then window: the caller needs the total to know whether a
        # next page exists, and it must not be inferred from a short page (which
        # also happens at an exact multiple).
        total_row = fetch_one(pool, _COUNT_CHUNKS_OF_LAW_SQL, {"law_id": law_id})
        total = int((total_row or {}).get("n") or 0)
        if start_at >= total and total:
            raise NotFound(
                f"offset {start_at} 超出该法规条文数 {total}；用 total_chunks 内的偏移重试。"
            )
        chunk_rows = fetch_all(
            pool, _CHUNKS_OF_LAW_WINDOW_SQL,
            {"law_id": law_id, "offset": start_at, "limit": window},
        )
        chunks = [_chunk_item(r) for r in chunk_rows]
        ensure_provenance(chunks)
        ensure_provenance([law])
        end_at = start_at + len(chunks)
        out: dict[str, Any] = {
            "law": law,
            "chunks": chunks,
            "count": len(chunks),
            "provenance": law["provenance"],
        }
        # 统一的续读约定（limits.py）：total 始终给出，不只在上限时给——
        # 调用方要靠它判断"还有没有下一页"，而短页在恰好整除时也会出现。
        out.update(limits.truncation_fields(
            total=total, offset=start_at, returned=len(chunks), unit="条",
            truncated_key="truncated", offset_key="chunk_offset",
            total_key="total_chunks"))
        return out

    @mcp.tool(annotations=annotations_for("law_semantic_search"))
    def law_semantic_search(query: str, top_k: int = 10) -> dict[str, Any]:
        """法条语义检索：Qwen3-Embedding-0.6B 查询向量 -> Qdrant law_chunks_v2。

        按语义相似度返回条级 chunk，逐条携带 provenance
        （source=law_db.law_chunks_v2, stable_id=point id, as_of=导出日期）。
        实现跟踪：lex-platform-v1 task 1.2。
        """
        q = (query or "").strip()
        if not q:
            raise Validation("validation：query 为空，语义检索需要非空查询文本")
        top_k = min(max(top_k, 1), _MAX_TOP_K)
        vector = get_embedder(settings).embed([q])[0]
        client = get_qdrant(settings)
        response = client.query_points(
            collection_name=settings.qdrant_collection,
            query=vector,
            limit=top_k,
            with_payload=True,
        )
        points = list(getattr(response, "points", []) or [])
        # as_of：chunk 行入库时间（逐点），Qdrant payload 不含该字段；
        # 未回表命中的点回退到 chunk 表全量导出快照（max(created_at)）。
        stable_ids = [
            "{}:{}".format((p.payload or {}).get("law_id"), (p.payload or {}).get("chunk_no", 0))
            for p in points
        ]
        pool = get_pool(_LAW_DB_ENV, settings.law_database_url)
        created = {
            r["chunk_id"]: r["created_at"]
            for r in fetch_all(pool, _CHUNK_CREATED_AT_SQL, {"ids": stable_ids})
        }
        snap_row = fetch_one(pool, _CHUNK_SNAPSHOT_SQL) or {}
        snapshot_as_of = _as_of_date(snap_row.get("m"))

        results = []
        for p in points:
            pl = p.payload or {}
            law_id = "" if pl.get("law_id") is None else str(pl.get("law_id"))
            chunk_no = int(pl.get("chunk_no") or 0)
            stable_id = f"{law_id}:{chunk_no}"
            item = {
                "law_id": law_id,
                "chunk_no": chunk_no,
                "title": pl.get("title"),
                "category": pl.get("category"),
                "law_type": pl.get("law_type"),
                "law_status": pl.get("law_status"),
                "publish": _iso(pl.get("publish")),
                "chunk_text": pl.get("chunk_text"),
                "score": round(float(p.score), 6),
                "point_id": str(p.id),
                "brand": _BRAND,
            }
            as_of = _as_of_date(created.get(stable_id)) or snapshot_as_of
            results.append(with_provenance(item, Provenance(_SOURCE_CHUNKS, stable_id, as_of)))
        ensure_provenance(results)
        return {"query": q, "results": results, "count": len(results)}

    @mcp.tool(annotations=annotations_for("law_reference"))
    def law_reference(citation: str) -> dict[str, Any]:
        """条级引用解析：把「《民法典》第一百四十三条」类引用解析到法规目录条级定位。

        无法解析时如实返回 not_found（含可读原因），绝不猜测。
        实现跟踪：lex-platform-v1 task 1.2。
        """
        title, art_no, _suffix_raw, numeral_raw = _parse_citation(citation or "")
        if not title:
            raise Validation(
                f"validation：无法从引用「{citation}」中识别法规标题"
                "（需《标题》或「标题第X条」形态）"
            )
        pool = get_pool(_LAW_DB_ENV, settings.law_database_url)
        law_row = _resolve_law(pool, title)
        law = _law_item(law_row)
        result: dict[str, Any] = {"query": (citation or "").strip(), "law": law}
        ensure_provenance([law])
        if art_no is None:
            result["article"] = None
            result["note"] = (
                f"引用未含条号，仅定位到法规《{law.get('title')}》(law_id={law.get('id')})"
            )
            return result

        target = f"第{numeral_raw}条" + (_suffix_raw or "")
        suffix_no = _num_to_int(_suffix_raw[1:]) if _suffix_raw else None
        chunk_rows = fetch_all(pool, _CHUNKS_OF_LAW_SQL, {"law_id": law_row["id"]})
        if not chunk_rows:
            raise NotFound(
                f"not_found：已定位《{law_row['title']}》(law_id={law_row['id']})，"
                f"但条级 chunk 库中无该法规内容，无法定位{target}"
            )
        joined = "".join(r["chunk_text"] or "" for r in chunk_rows)
        hit = _find_article(joined, art_no, suffix_no)
        if hit is None:
            raise NotFound(
                f"not_found：已定位《{law_row['title']}》(law_id={law_row['id']})，"
                f"但全文中未找到{target}（可能该法规无此条号；不猜测）"
            )
        m_start, m_end = hit
        end = _article_end(joined, m_end, art_no)
        raw = joined[m_start:end] if end is not None else joined[m_start:m_start + _EXCERPT_MAX_CHARS]
        excerpt = raw[:_EXCERPT_MAX_CHARS]
        host = _chunk_containing(chunk_rows, m_start) or chunk_rows[-1]
        result["article"] = {
            "no": art_no,
            "display": joined[m_start:m_end],
            "excerpt": excerpt,
            "truncated": len(raw) > _EXCERPT_MAX_CHARS,
        }
        result["chunk"] = _chunk_item(host)
        ensure_provenance([result["chunk"]])
        # 顶层 provenance = 条级定位锚（所在 chunk）的三件套。
        result["provenance"] = result["chunk"]["provenance"]
        return result


# ---------------------------------------------------------------- 条级定位（chunk 连续拼接后精确匹配）

def _article_matches(text: str):
    """yield (start, end, no, suffix_no)；suffix_no 为「之N」的 N（无则 None）。"""
    for m in _ARTICLE_RE.finditer(text):
        yield m.start(), m.end(), _num_to_int(m.group(1)), (
            _num_to_int(m.group(2)) if m.group(2) else None
        )


def _find_article(text: str, art_no: int, suffix_no: int | None) -> tuple[int, int] | None:
    """全文中第一个条号严格等于目标（含「之N」完全匹配）的出现。

    依中文法条"引用皆向后"的行文惯例，首个出现即条头；
    目录不列条号，故不与目录冲突。
    """
    for start, end, no, suffix in _article_matches(text):
        if no == art_no and suffix == suffix_no:
            return start, end
    return None


def _article_end(text: str, art_end: int, art_no: int) -> int | None:
    """条文终点 = 其后第一个条号更大、或同条号但「之N」不同的标记起点；无则 None。"""
    for start, _end, no, suffix_no in _article_matches(text):
        if start < art_end:
            continue
        if no > art_no or (no == art_no and suffix_no is not None):
            return start
    return None


def _chunk_containing(chunk_rows: list[dict[str, Any]], offset: int) -> dict[str, Any] | None:
    """按 chunk_no 累计长度映射拼接文本偏移 -> 所在 chunk 行。"""
    cursor = 0
    for row in chunk_rows:
        length = len(row.get("chunk_text") or "")
        if cursor <= offset < cursor + length:
            return row
        cursor += length
    return None
