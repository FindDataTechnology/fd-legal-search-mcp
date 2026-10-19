"""类案域：裁判文书检索与正文读取（wenshu_db + RustFS zstd 块）。

wenshu_search / wenshu_read 为 business-mcp 等价迁移面（签名逐字对齐）；
实现移植自 fd-find-data-business-mcp 的 tools_wenshu.py（检索）与
wenshu_body.py（读块），lex-platform-v1 task 1.3 打通。

实现要点（与 business-mcp 的差异与理由，均经 live EXPLAIN 验证）：

- 检索排序 exact > prefix > contains 在候选样本内用 Python 完成。37M 行大表上
  全局 ``ORDER BY rank, 标题`` 会迫使 planner 放弃 trgm 索引走并行顺序扫描
  （实测 74s）；这里改为单轮 contains 候选查询（``标题 ILIKE '%q%'``），事务内
  ``SET LOCAL enable_seqscan = off`` 锁定位图索引路径，``LIMIT 2000`` 提前终止：
  宽泛词 ~0.8-1.0s，稀疏词 35-550ms，空结果 35ms，任何词形都不会全表扫。
  候选触顶时结果如实标注（note + truncated），收窄建议同 business-mcp
  （start_year/end_year 走 btree 判决年份索引）。
- doc_type/province 过滤先经 dict_texts 解析为 ref_key（解析不到 = 该字典值
  不存在，如实返回空结果），候选阶段用原始 ``*引用`` 列过滤，详情阶段再做
  LEFT JOIN 解码——缺字段如实为 null，绝不猜测（与 business-mcp 相同）。
- 正文块读取移植 wenshu_body.py 的双写方嗅探：zstd magic 开头 = blocks.js
  写方（解压后首行 BLOCK_MAGIC="WENSHUBLOCK1"，offsets 为 docs+1 个累计
  端点）；否则为历史 Windows 写方（u32-BE 头长 + JSON {docs, offsets} +
  zstd payload，offsets 为每篇绝对 [start, byteLen]）。本仓 live 实测
  （2026-10-05，桶 wenshu）：现网对象均为历史写方形态（如
  blocks/2025/02/000040.zst：头 107361B、2157 篇、raw 8MB）；blocks.js
  形态按写方约定保留嗅探支持。
- as_of 口径：逐条取该行 ``发布时间``（实测 0.02% 抽样 n=7555 全非空），
  缺失时回退 ``判决时间``，再缺失如实 "unknown"——不用无法成立的日期。
- 正文上限：超过 200,000 字符截断并如实标注（实测单篇最大 ~41KB，正常
  情况不会触发）；不做磁盘缓存（避免缓存失效面，读块直连）。
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import zstandard

from fastmcp import FastMCP

from .config import Settings
from .db import fetch_all, get_pool, get_rustfs
from .errors import NotFound, UpstreamUnavailable
from .provenance import Provenance, ensure_provenance, with_provenance
from .textmatch import escape_like, rank
from .annotations import annotations_for
from . import limits

logger = logging.getLogger(__name__)

_BRAND = "finddata"
_WENSHU_SOURCE = "wenshu_db.judgments"

# 上限的唯一真源在 limits.py（mcp-result-contract）
_MAX_LIMIT = limits.MAX_LIST_ITEMS

#: 候选采样上限：contains 查询一次最多取回的命中数（提前终止，防宽泛词拖垮）。
_CANDIDATE_CAP = 2000

#: 正文返回上限（字符）；超出截断并如实标注 + 给出续读游标。
#: 20 万字符的中文正文约等于 20 万 token——足以独占一次对话的上下文预算，
#: 而实测单篇最大约 41KB，也就是说旧上限只在最坏情况下起作用、一旦起作用
#: 就是把调用方打爆。默认值取「一次放进上下文仍然可读」的量级；需要更长正文
#: 的调用方用 offset 续读，而不是让单次响应变长。
_MAX_CONTENT_CHARS = limits.DEFAULT_BODY_CHARS

#: 单次窗口的硬上限：调用方可以把 ``max_chars`` 调大，但不能无界——那等于
#: 把上限这件事取消掉，而它存在的理由正是保护调用方的上下文。
_MAX_WINDOW_CHARS = limits.MAX_BODY_CHARS

#: Internal coordinates and raw dictionary refs that must never reach a
#: customer payload. SQL 只在过滤/定位时用它们，投影白名单 + 本黑名单双保险。
_FORBIDDEN_KEYS = frozenset({
    "id_origin", "block_key", "block_ordinal", "src_id",
    "法院引用", "案由引用", "省引用", "市引用", "案件类型引用",
    "文书类型引用", "审理程序引用", "案例级别引用", "法院级别引用", "法条引用",
})

#: Whitelist: search hits carry only these row fields — decoded dictionary
#: text, never the raw refs, never the block coordinates.
_SEARCH_FIELDS = (
    "doc_id", "案号", "标题", "法院", "案由", "省", "市", "案件类型",
    "文书类型", "审理程序", "法院级别", "判决年份", "判决结果", "发布时间",
)

#: Reads add the two remaining dates and the raw 正文形态 as ``body_shape``.
_READ_FIELDS = _SEARCH_FIELDS + ("判决时间", "受理时间", "body_shape")

_NO_BODY_NOTE = "该篇文书仅元数据入库（正文形态为 none，无正文块），现仅提供元数据。"

_BODY_UNAVAILABLE_NOTE = (
    "正文块暂时不可读（对象存储访问失败），元数据仍完整，请稍后重试。"
)

#: Decode JOINs shared by search and read (alias -> dict 列).
_DECODE_JOINS = (
    ('df', "法院", "法院引用"),
    ('da', "案由", "案由引用"),
    ('dp', "省", "省引用"),
    ('dc', "市", "市引用"),
    ('dct', "案件类型", "案件类型引用"),
    ('dwt', "文书类型", "文书类型引用"),
    ('dpr', "审理程序", "审理程序引用"),
    ('dcl', "法院级别", "法院级别引用"),
)


def _decode_join_sql() -> str:
    return "\n".join(
        f'LEFT JOIN dict_texts {alias} ON {alias}."列" = \'{col}\''
        f' AND {alias}.ref_key = j."{ref}"'
        for alias, col, ref in _DECODE_JOINS
    )


def _as_of(row: dict[str, Any]) -> str:
    """逐条数据口径日期：发布时间 → 判决时间 → "unknown"（绝不编日期）。"""
    for key in ("发布时间", "判决时间"):
        value = row.get(key)
        if value:
            return str(value)
    return "unknown"


def _fetch_candidates(pool: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """候选查询：同一事务内 ``SET LOCAL enable_seqscan = off`` 锁定位图路径。

    37M 行大表上 planner 在宽泛词会选顺序扫描提前终止（平时很快），但空/稀疏
    结果会全表扫（实测 74s）；关掉 seqscan 后任何词形都走 trgm/年份位图，
    ``LIMIT`` 提前终止，最坏 ~1s（live EXPLAIN ANALYZE 实测，见模块 docstring）。
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------------
# RustFS zstd 块读取（移植 business-mcp wenshu_body.py，双写方嗅探，fail-soft）
# --------------------------------------------------------------------------

#: First line of every decompressed block (writer: lib/wenshu/blocks.js).
BLOCK_MAGIC = "WENSHUBLOCK1"

#: Zstd frame magic — an object starting with these bytes is a whole-object
#: zstd stream (blocks.js writer); anything else is the historical
#: length-prefixed-header layout.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _fetch_compressed(settings: Settings, block_key: str) -> bytes | None:
    """GET the zstd block object; None (logged) on any failure."""
    try:
        client = get_rustfs(settings)
        obj = client.get_object(Bucket=settings.rustfs_bucket, Key=block_key)
        return obj["Body"].read()
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("wenshu_body: GET %s failed: %s", block_key, type(exc).__name__)
        return None


def _zstd_read(blob: bytes, block_key: str, what: str) -> bytes | None:
    """zstd-decompress ``blob``; None (logged) on any failure.

    stream_reader (not decompress()) so frames without a content-size
    header also decompress — neither writer's framing is guaranteed to
    carry one.
    """
    try:
        return zstandard.ZstdDecompressor() \
            .stream_reader(io.BytesIO(blob)).read()
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("wenshu_body: zstd decompress %s (%s) failed: %s",
                       block_key, what, type(exc).__name__)
        return None


def _parse_block(compressed: bytes, block_key: str) -> tuple[list, Any] | None:
    """Parse a fetched object into (docs, slicer).

    ``slicer(i)`` returns the raw payload bytes of doc ``i``. Recognizes
    both writer formats; returns None (logged) on any malformed input.
    """
    if compressed[:4] == _ZSTD_MAGIC:
        return _parse_blocks_js(compressed, block_key)
    return _parse_legacy(compressed, block_key)


def _parse_blocks_js(compressed: bytes, block_key: str) -> tuple[list, Any] | None:
    """blocks.js layout: whole-object zstd of magic line + JSON + payload."""
    dec = _zstd_read(compressed, block_key, "whole object")
    if dec is None:
        return None
    nl = dec.find(b"\n")
    if nl == -1 or dec[:nl] != BLOCK_MAGIC.encode("utf-8"):
        logger.warning("wenshu_body: %s bad magic line (expected %s)",
                       block_key, BLOCK_MAGIC)
        return None
    start = nl + 1
    nl2 = dec.find(b"\n", start)
    if nl2 == -1:
        logger.warning("wenshu_body: %s truncated (no header newline)", block_key)
        return None
    try:
        header = json.loads(dec[start:nl2].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("wenshu_body: %s header is not valid JSON", block_key)
        return None
    docs = header.get("docs") if isinstance(header, dict) else None
    offsets = header.get("offsets") if isinstance(header, dict) else None
    if (not isinstance(docs, list) or not isinstance(offsets, list)
            or len(offsets) != len(docs) + 1):
        logger.warning("wenshu_body: %s malformed header (docs/offsets)", block_key)
        return None
    payload_start = nl2 + 1

    def slicer(i: int) -> bytes | None:
        a, b = offsets[i], offsets[i + 1]
        if (not isinstance(a, int) or not isinstance(b, int)
                or a < 0 or b < a or payload_start + b > len(dec)):
            logger.warning("wenshu_body: %s doc slice out of bounds (doc %s)",
                           block_key, docs[i])
            return None
        return dec[payload_start + a:payload_start + b]

    return docs, slicer


def _parse_legacy(compressed: bytes, block_key: str) -> tuple[list, Any] | None:
    """Historical Windows writer: u32-BE header length + JSON + zstd payload.

    现网主流形态（本仓 2026-10-05 实测 blocks/2025/02/000040.zst 等）：
    header JSON 的 ``offsets`` 为每篇绝对 ``[start, byte_len]`` 对，指向
    解压后 payload。
    """
    if len(compressed) < 5:
        logger.warning("wenshu_body: %s too short for legacy header", block_key)
        return None
    plen = int.from_bytes(compressed[:4], "big")
    if plen <= 0 or 4 + plen >= len(compressed):
        logger.warning("wenshu_body: %s implausible legacy header length %d",
                       block_key, plen)
        return None
    try:
        header = json.loads(compressed[4:4 + plen].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("wenshu_body: %s header is not valid JSON", block_key)
        return None
    docs = header.get("docs") if isinstance(header, dict) else None
    offsets = header.get("offsets") if isinstance(header, dict) else None
    if (not isinstance(docs, list) or not isinstance(offsets, list)
            or len(offsets) != len(docs)):
        logger.warning("wenshu_body: %s malformed header (docs/offsets)", block_key)
        return None
    payload = _zstd_read(compressed[4 + plen:], block_key, "payload section")
    if payload is None:
        return None

    def slicer(i: int) -> bytes | None:
        pair = offsets[i]
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(v, int) for v in pair)
                or pair[0] < 0 or pair[1] < 0
                or pair[0] + pair[1] > len(payload)):
            logger.warning("wenshu_body: %s doc slice out of bounds (doc %s)",
                           block_key, docs[i])
            return None
        return payload[pair[0]:pair[0] + pair[1]]

    return docs, slicer


def _decode_doc(raw: bytes | None, doc_id: str, block_key: str) -> str | None:
    """utf-8 decode one sliced payload; None (logged) on failure."""
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("wenshu_body: %s doc %s payload is not valid utf-8",
                       block_key, doc_id)
        return None


def fetch_judgment_body(settings: Settings, block_key: str, doc_id: str) -> str | None:
    """Read one judgment's body text from its RustFS zstd block.

    Args:
        settings: 运行配置（RustFS 端点/凭据/桶）。
        block_key: internal object coordinate from wenshu_db.judgments
            (e.g. ``blocks/2024/09/000711.zst``). ``block_key IS NULL``
            rows have no body block at all.
        doc_id: the judgment's primary key, looked up inside the block
            header.

    Returns the document's utf-8 text, or ``None`` when anything at all
    goes wrong — unreachable object store, missing credentials, corrupt
    zstd, bad block format, or the doc not being in the block. Never
    raises.
    """
    if not block_key or not doc_id:
        return None
    compressed = _fetch_compressed(settings, block_key)
    if compressed is None:
        return None
    parsed = _parse_block(compressed, block_key)
    if parsed is None:
        return None
    docs, slicer = parsed
    try:
        idx = docs.index(doc_id)
    except ValueError:
        # Expected miss (doc never in this block), not a malfunction.
        logger.debug("wenshu_body: %s does not contain doc %s", block_key, doc_id)
        return None
    return _decode_doc(slicer(idx), doc_id, block_key)


def register(mcp: FastMCP, settings: Settings) -> None:

    @mcp.tool(annotations=annotations_for("wenshu_search"))
    def wenshu_search(
        title_query: str,
        doc_type: str | None = None,
        province: str | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search judgment titles (3,700万篇裁判文书), ranked exact > prefix > contains.

        案号内嵌于标题（如「（2024）京01民初1号」），无需单独案号参数。
        结果逐条携带 provenance（source=wenshu_db.judgments, stable_id=doc_id）。
        与 business-mcp wenshu_search 等价迁移（lex-platform-v1 task 7.1）。
        """
        q = (title_query or "").strip()
        if not q:
            return {"results": [], "count": 0, "truncated": False, "brand": _BRAND}
        limit = min(max(int(limit), 1), _MAX_LIMIT)
        needle = q.lower()
        esc = escape_like(q)
        try:
            pool = get_pool("FDLEX_WENSHU_DATABASE_URL", settings.wenshu_database_url)

            # 可选过滤先解析为 ref_key（字典里没有 = 该值不存在，如实空结果）。
            params: dict[str, Any] = {
                "pattern": f"%{esc}%",
                "cap": _CANDIDATE_CAP,
            }
            filter_sql = ""
            dtype = (doc_type or "").strip() or None
            if dtype is not None:
                refs = fetch_all(
                    pool,
                    'SELECT ref_key FROM dict_texts WHERE "列" = %(col)s'
                    " AND txt = %(val)s",
                    {"col": "文书类型", "val": dtype},
                )
                if not refs:
                    return {"results": [], "count": 0, "truncated": False,
                            "brand": _BRAND}
                params["doc_type_refs"] = [r["ref_key"] for r in refs]
                filter_sql += '  AND j."文书类型引用" = ANY(%(doc_type_refs)s)\n'
            prov = (province or "").strip() or None
            if prov is not None:
                refs = fetch_all(
                    pool,
                    'SELECT ref_key FROM dict_texts WHERE "列" = %(col)s'
                    " AND txt = %(val)s",
                    {"col": "省", "val": prov},
                )
                if not refs:
                    return {"results": [], "count": 0, "truncated": False,
                            "brand": _BRAND}
                params["province_refs"] = [r["ref_key"] for r in refs]
                filter_sql += '  AND j."省引用" = ANY(%(province_refs)s)\n'
            if start_year is not None:
                params["start_year"] = int(start_year)
                filter_sql += '  AND j."判决年份" >= %(start_year)s\n'
            if end_year is not None:
                params["end_year"] = int(end_year)
                filter_sql += '  AND j."判决年份" <= %(end_year)s\n'

            # 候选阶段：窄列（doc_id/标题）+ 位图路径 + LIMIT 提前终止。
            rows = _fetch_candidates(
                pool,
                'SELECT j."doc_id" AS doc_id, j."标题" AS 标题\n'
                "FROM judgments j\n"
                'WHERE j."标题" ILIKE %(pattern)s\n' + filter_sql +
                "LIMIT %(cap)s",
                params,
            )
            sampled = len(rows) >= _CANDIDATE_CAP
            total_candidates = len(rows)
            rows = sorted(rows, key=lambda r: (rank(r.get("标题"), needle),
                                               r.get("标题") or ""))[:limit]
            if not rows:
                return {"results": [], "count": 0, "truncated": False,
                        "brand": _BRAND}

            # 详情阶段：按 PK 取回解码后的白名单字段（无全扫路径）。
            detail_rows = fetch_all(
                pool,
                'SELECT j."doc_id" AS doc_id, j."案号" AS 案号, j."标题" AS 标题,\n'
                "       df.txt AS 法院, da.txt AS 案由, dp.txt AS 省, dc.txt AS 市,\n"
                "       dct.txt AS 案件类型, dwt.txt AS 文书类型, dpr.txt AS 审理程序,\n"
                "       dcl.txt AS 法院级别,\n"
                '       j."判决年份" AS 判决年份, j."判决结果" AS 判决结果,\n'
                '       j."判决时间"::date::text AS 判决时间,\n'
                '       j."发布时间"::date::text AS 发布时间,\n'
                "       (COALESCE(j.\"正文形态\", 'none') <> 'none'"
                ' AND j."block_key" IS NOT NULL) AS has_body\n'
                "FROM judgments j\n" + _decode_join_sql() +
                '\nWHERE j."doc_id" = ANY(%(ids)s)',
                {"ids": [r["doc_id"] for r in rows]},
            )
        except UpstreamUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — 上游故障如实回报，不编数据
            raise UpstreamUnavailable(
                f"wenshu_db 查询失败：{type(exc).__name__}: {exc}"
            ) from exc

        by_id = {r["doc_id"]: r for r in detail_rows}
        items: list[dict[str, Any]] = []
        for cand in rows:
            row = by_id.get(cand["doc_id"])
            if row is None:
                logger.warning("wenshu_search: candidate %s vanished before detail",
                               cand["doc_id"])
                continue
            item = {k: row.get(k) for k in _SEARCH_FIELDS}
            item["has_body"] = bool(row.get("has_body"))
            item["brand"] = _BRAND
            item = {k: v for k, v in item.items() if k not in _FORBIDDEN_KEYS}
            items.append(with_provenance(
                item, Provenance(_WENSHU_SOURCE, str(cand["doc_id"]), _as_of(row))))
        ensure_provenance(items)
        out: dict[str, Any] = {
            "results": items,
            "count": len(items),
            "truncated": sampled or total_candidates > limit,
            "brand": _BRAND,
        }
        if sampled:
            out["note"] = (
                f"命中超出内部候选采样上限 {_CANDIDATE_CAP}，结果为采样内按 exact>"
                "prefix>contains 与标题排序；宽泛检索词可用 start_year/end_year"
                "（走判决年份索引）收窄。存量快照并非全量语料。"
            )
        return out

    @mcp.tool(annotations=annotations_for("wenshu_read"))
    def wenshu_read(doc_id: str, offset: int = 0, max_chars: int = _MAX_CONTENT_CHARS) -> dict[str, Any]:
        """Read one judgment's full text (zstd blocks on RustFS).

        正文按块解压读取；条目存在而正文缺失时如实标注，绝不编造正文。
        与 business-mcp wenshu_read 等价迁移（lex-platform-v1 task 7.1）。

        长正文按窗口返回：单次最多 ``max_chars`` 字符（默认 12000，上限 40000）。
        超出时 ``content_truncated=true`` 且给 ``next_offset``——把它作为
        ``offset`` 再调一次即可续读，分段按字符切分，拼接后与原文逐字符一致。
        """
        did = (doc_id or "").strip()
        if not did:
            raise NotFound("wenshu_read：doc_id 为空，无法定位文书。")
        try:
            pool = get_pool("FDLEX_WENSHU_DATABASE_URL", settings.wenshu_database_url)
            rows = fetch_all(
                pool,
                'SELECT j."doc_id" AS doc_id, j."案号" AS 案号, j."标题" AS 标题,\n'
                "       df.txt AS 法院, da.txt AS 案由, dp.txt AS 省, dc.txt AS 市,\n"
                "       dct.txt AS 案件类型, dwt.txt AS 文书类型, dpr.txt AS 审理程序,\n"
                "       dcl.txt AS 法院级别,\n"
                '       j."判决年份" AS 判决年份, j."判决结果" AS 判决结果,\n'
                '       j."判决时间"::date::text AS 判决时间,\n'
                '       j."发布时间"::date::text AS 发布时间,\n'
                '       j."受理时间"::date::text AS 受理时间,\n'
                '       j."正文形态" AS body_shape, j."block_key"\n'
                "FROM judgments j\n" + _decode_join_sql() +
                '\nWHERE j."doc_id" = %(doc_id)s',
                {"doc_id": did},
            )
        except UpstreamUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 — 上游故障如实回报，不编数据
            raise UpstreamUnavailable(
                f"wenshu_db 查询失败：{type(exc).__name__}: {exc}"
            ) from exc
        if not rows:
            raise NotFound(
                f"wenshu_db.judgments 中不存在 doc_id={did}（未命中，如实回报）。"
            )
        row = rows[0]
        out: dict[str, Any] = {k: row.get(k) for k in _READ_FIELDS}
        out["brand"] = _BRAND
        block_key = row.get("block_key")
        if (row.get("body_shape") or "none") == "none" or not block_key:
            out["content_available"] = False
            out["note"] = _NO_BODY_NOTE
        else:
            content = fetch_judgment_body(settings, block_key, did)
            if content is None:
                out["content_available"] = False
                out["note"] = _BODY_UNAVAILABLE_NOTE
            else:
                out["content_available"] = True
                out["total_chars"] = len(content)
                window = limits.clamp(max_chars, low=1, high=_MAX_WINDOW_CHARS)
                start_at = max(0, int(offset))
                if start_at >= len(content) and content:
                    # Reading past the end is a caller error worth naming, not a
                    # silent empty string that looks like "the document ends here".
                    raise NotFound(
                        f"offset {start_at} 超出正文长度 {len(content)}；"
                        "用 total_chars 上的偏移重试。"
                    )
                chunk, end_at, has_more = limits.slice_window(
                    content, start_at, window)
                out["content"] = chunk
                out.update(limits.truncation_fields(
                    total=len(content), offset=start_at, returned=len(chunk)))
        out = {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}
        out = with_provenance(out, Provenance(_WENSHU_SOURCE, did, _as_of(row)))
        ensure_provenance([out])
        return out
