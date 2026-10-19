"""法条域四工具的离线行为测试（不触网：数据源一律 monkeypatch）。

live 冒烟（真实 law_db / Qdrant / Embedding）在 scripts/smoke_law.py；
本文件负责：business-mcp 迁移语义（排序/去重/截断）、溯源契约、
中文条号解析、条级定位与如实 not_found。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import fd_legal_search_mcp.tools_law as tl
from fd_legal_search_mcp.config import Settings
from fd_legal_search_mcp.errors import NotFound, Validation
from fd_legal_search_mcp.server import create_server

_INGEST_TS = datetime(2026, 9, 9, 8, 4, 29, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(
        law_database_url="postgresql://fake/law_db",
        wenshu_database_url=None,
        qdrant_url="http://fake:6333",
        qdrant_collection="law_chunks_v2",
        qdrant_api_key=None,
        rustfs_endpoint=None,
        rustfs_bucket="wenshu",
        rustfs_access_key=None,
        rustfs_secret_key=None,
        embed_backend="api",
        embed_api_url="https://fake/v1",
        embed_api_key="k",
        embed_model="fake-model",
        embed_dim=1024,
    )


def _law_row(**over: Any) -> dict[str, Any]:
    row = {
        "id": 69695,
        "title": "中华人民共和国民法典",
        "category": "法律",
        "type": "法律",
        "status": "有效",
        "publish": "2020-05-28 00:00:00",
        "expiry": None,
        "created_at": _INGEST_TS,
    }
    row.update(over)
    return row


def _mini_chunks(law_id: int = 69695) -> list[dict[str, Any]]:
    """三条 chunk 的小型法规全文；第一百四十三条跨 chunk 1 -> 2，验证续块拼接。"""
    texts = [
        "中华人民共和国民法典\n（2020年5月28日第十三届全国人民代表大会第三次会议通过）\n"
        "第一编　总则\n第一章　基本规定\n",
        "第一条　为了保护民事主体的合法权益，调整民事关系。\n"
        "第二条　民法调整平等主体之间的人身关系和财产关系。\n"
        "第一百四十三条　具备下列条件的民事法律行为有效：\n（一）行为人具有相应的民事行为能力；\n",
        "（二）意思表示真实；\n（三）不违反法律、行政法规的强制性规定。\n"
        "第一百四十四条　无民事行为能力人实施的民事法律行为无效。\n",
    ]
    rows: list[dict[str, Any]] = []
    offset = 0
    for no, text in enumerate(texts):
        rows.append(
            {
                "chunk_id": f"{law_id}:{no}",
                "law_id": law_id,
                "chunk_no": no,
                "chunk_text": text,
                "char_start": offset,
                "char_end": offset + len(text),
                "created_at": _INGEST_TS,
            }
        )
        offset += len(text)
    return rows


class _FakeEmbedder:
    dim = 1024

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return [[0.01] * self.dim for _ in texts]


class _FakeQdrant:
    def __init__(self, points: list[SimpleNamespace]) -> None:
        self.points = points
        self.calls: list[dict[str, Any]] = []

    def query_points(self, **kwargs: Any):
        self.calls.append(kwargs)
        return SimpleNamespace(points=self.points)


def _qdrant_point(law_id: str, chunk_no: int, score: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=int(law_id) * 1048576 + chunk_no,
        score=score,
        payload={
            "law_id": law_id,
            "chunk_no": chunk_no,
            "title": "中华人民共和国民法典",
            "category": "法律",
            "law_type": "法律",
            "law_status": "有效",
            "publish": "2020-05-28 00:00:00",
            "chunk_text": "第一百四十三条　具备下列条件的民事法律行为有效。",
        },
    )


@pytest.fixture
def wired(monkeypatch):
    """按 SQL 形态路由的假数据源；返回 install() 以便各用例注入行。"""

    def install(
        *,
        laws_rows: list[dict[str, Any]] | None = None,
        search_rows: list[dict[str, Any]] | None = None,
        chunks_by_law: dict[int, list[dict[str, Any]]] | None = None,
        qdrant_points: list[SimpleNamespace] | None = None,
        snapshot: Any = _INGEST_TS,
    ):
        laws_rows = laws_rows or []
        search_rows = search_rows
        chunks_by_law = chunks_by_law or {}
        embedder, qdrant = _FakeEmbedder(), _FakeQdrant(qdrant_points or [])

        def route(sql: str, params: dict[str, Any]):
            if "DISTINCT ON (tkey)" in sql:  # law_search
                return list(search_rows or [])
            if "FROM laws WHERE id" in sql:  # law_read
                return [r for r in laws_rows if r["id"] == params["law_id"]]
            if "title ILIKE" in sql:  # _resolve_law
                needle = str(params["pattern"]).strip("%")
                return [r for r in laws_rows if needle in (r["title"] or "")]
            if "count(*) AS n FROM law_chunks_v2" in sql:  # law_read 窗口计数
                return [{"n": len(chunks_by_law.get(params["law_id"], []))}]
            if "FROM law_chunks_v2 WHERE law_id" in sql:  # law_read / law_reference
                rows = list(chunks_by_law.get(params["law_id"], []))
                # 窗口查询带 OFFSET/LIMIT；按 chunk_no 排序后切片，模拟 SQL 行为。
                if "OFFSET" in sql:
                    off = int(params.get("offset") or 0)
                    lim = int(params.get("limit") or len(rows) or 1)
                    rows = sorted(rows, key=lambda r: r["chunk_no"])[off:off + lim]
                return rows
            if "chunk_id = ANY" in sql:  # semantic as_of
                want = set(params["ids"])
                return [
                    r for rows in chunks_by_law.values() for r in rows if r["chunk_id"] in want
                ]
            if "max(created_at)" in sql:  # semantic snapshot
                return [{"m": snapshot}]
            raise AssertionError(f"假数据源未覆盖的 SQL: {sql[:120]}")

        monkeypatch.setattr(tl, "get_pool", lambda env, url: object())
        monkeypatch.setattr(tl, "fetch_all", lambda pool, sql, params=(): route(sql, params))
        monkeypatch.setattr(
            tl, "fetch_one", lambda pool, sql, params=(): (route(sql, params) or [None])[0]
        )
        monkeypatch.setattr(tl, "get_qdrant", lambda s: qdrant)
        monkeypatch.setattr(tl, "get_embedder", lambda s: embedder)
        return create_server(_settings()), embedder, qdrant

    return install


# ---------------------------------------------------------------- 中文数字 / 引用解析（纯函数）

@pytest.mark.parametrize(
    "numeral,expected",
    [
        ("十", 10),
        ("十四", 14),
        ("二十", 20),
        ("九十九", 99),
        ("一百", 100),
        ("一百四十三", 143),
        ("二百", 200),
        ("两百", 200),
        ("一千零一", 1001),
        ("一万二千", 12000),
        ("143", 143),
        ("１００", 100),
    ],
)
def test_num_to_int(numeral: str, expected: int):
    assert tl._num_to_int(numeral) == expected


@pytest.mark.parametrize(
    "citation,expected",
    [
        ("《民法典》第一百四十三条", ("民法典", 143, None, "一百四十三")),
        ("《刑法》第二十条", ("刑法", 20, None, "二十")),
        ("《公司法》第3条", ("公司法", 3, None, "3")),
        ("《劳动法》第１００条", ("劳动法", 100, None, "１００")),
        ("《刑法》第十条之一", ("刑法", 10, "之一", "十")),
        ("《民法典》第一千零一条", ("民法典", 1001, None, "一千零一")),
        ("民法典第二十条", ("民法典", 20, None, "二十")),
        ("《民法典》", ("民法典", None, None, None)),
        ("民法典", ("民法典", None, None, None)),
    ],
)
def test_parse_citation(citation: str, expected):
    assert tl._parse_citation(citation) == expected


def test_parse_citation_without_title_yields_none_title():
    # 「第一百四十三条」这种只有条号的引用：标题为 None，由 law_reference 层抛 Validation
    assert tl._parse_citation("第一百四十三条")[0] is None


# ---------------------------------------------------------------- law_search

async def test_law_search_ranks_exact_first_and_carries_provenance(wired):
    server, _, _ = wired(
        search_rows=[
            _law_row(id=57085, title="互助土族自治县施行《中华人民共和国民法典》结婚年龄的变通规定"),
            _law_row(id=69695, title="中华人民共和国民法典"),
        ]
    )
    async with Client(server) as client:
        res = await client.call_tool("law_search", {"title_query": "中华人民共和国民法典"})
    data = res.data
    assert data["count"] == 2 and data["truncated"] is False
    first = data["results"][0]
    assert first["id"] == 69695  # exact > contains
    prov = first["provenance"]
    assert prov["source"] == "law_db.laws"
    assert prov["stable_id"] == "69695"
    assert prov["as_of"] == _INGEST_TS.date().isoformat()
    assert first["brand"] == "finddata"


async def test_law_search_dedupes_to_authoritative_row(wired):
    server, _, _ = wired(
        search_rows=[
            _law_row(id=18726, title="中华人民共和国刑法", category="重下载", status="有效",
                     publish="2020-12-26 00:00:00"),
            _law_row(id=1, title="中华人民共和国刑法", category="法律", status="有效",
                     publish="2020-12-26 00:00:00"),
        ]
    )
    async with Client(server) as client:
        res = await client.call_tool("law_search", {"title_query": "刑法"})
    data = res.data
    assert data["count"] == 1
    assert data["results"][0]["id"] == 1  # 真实类别 > 爬虫批次
    assert data["results"][0]["category"] == "法律"


async def test_law_search_batch_category_filter_matches_nothing_and_synonym_expands(wired):
    captured: dict[str, Any] = {}

    def spy_fetch_all(pool, sql, params=()):
        captured.update(params)
        return []

    server, _, _ = wired(search_rows=[])
    import fd_legal_search_mcp.tools_law as tl_mod

    original = tl_mod.fetch_all
    tl_mod.fetch_all = spy_fetch_all
    try:
        async with Client(server) as client:
            batch = await client.call_tool(
                "law_search", {"title_query": "x", "category": "新法速递"}
            )
            assert batch.data == {"results": [], "count": 0, "truncated": False}
            await client.call_tool("law_search", {"title_query": "x", "category": "地方法规"})
    finally:
        tl_mod.fetch_all = original
    assert set(captured["categories"]) == {"地方法规", "地方性法规"}


async def test_law_search_truncates_after_dedupe(wired):
    rows = [_law_row(id=i, title=f"测试法规{i:02d}") for i in range(8)]
    server, _, _ = wired(search_rows=rows)
    async with Client(server) as client:
        res = await client.call_tool("law_search", {"title_query": "测试法规", "limit": 3})
    data = res.data
    assert data["count"] == 3 and data["truncated"] is True
    assert [r["id"] for r in data["results"]] == [0, 1, 2]


async def test_law_search_empty_query_returns_empty(wired):
    server, _, _ = wired(search_rows=[_law_row()])
    async with Client(server) as client:
        res = await client.call_tool("law_search", {"title_query": "   "})
    assert res.data == {"results": [], "count": 0, "truncated": False}


async def test_law_search_signatures_match_business_mcp_surface(wired):
    server, _, _ = wired()
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    # fastmcp 2.x 用 input_schema、3.x 用 inputSchema（同一字段的两次改名）。
    # 两版都在支持区间内（mcp-protocol-hardening 组 8），故按属性名探测，
    # 不钉死某一版——否则测试结果取决于装的是哪版。
    props = {
        name: sorted(
            (getattr(t, "input_schema", None) or t.inputSchema or {}).get("properties", {})
        )
        for name, t in tools.items()
    }
    assert props["law_search"] == ["category", "limit", "title_query"]
    # law_read 带两个续读参数：条文按窗口返回，避免大法典一次性独占调用方上下文。
    assert props["law_read"] == ["law_id", "limit", "offset"]
    assert props["law_semantic_search"] == ["query", "top_k"]
    assert props["law_reference"] == ["citation"]


# ---------------------------------------------------------------- law_read

async def test_law_read_returns_law_and_chunks_with_provenance(wired):
    chunks = _mini_chunks()
    server, _, _ = wired(laws_rows=[_law_row()], chunks_by_law={69695: chunks})
    async with Client(server) as client:
        res = await client.call_tool("law_read", {"law_id": 69695})
    data = res.data
    assert data["law"]["title"] == "中华人民共和国民法典"
    assert data["law"]["provenance"]["source"] == "law_db.laws"
    assert data["count"] == 3
    assert [c["chunk_no"] for c in data["chunks"]] == [0, 1, 2]
    for c in data["chunks"]:
        assert c["provenance"]["source"] == "law_db.law_chunks_v2"
        assert c["provenance"]["stable_id"] == c["chunk_id"]
        assert c["provenance"]["as_of"] == _INGEST_TS.date().isoformat()


async def test_law_read_unknown_id_is_honest_not_found(wired):
    server, _, _ = wired(laws_rows=[])
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("law_read", {"law_id": 999999999})
    assert "not_found" in str(excinfo.value)
    assert "999999999" in str(excinfo.value)


# ---------------------------------------------------------------- law_semantic_search

async def test_semantic_search_returns_scored_chunks_with_provenance(wired):
    chunks = _mini_chunks()
    server, embedder, qdrant = wired(
        chunks_by_law={69695: chunks},
        qdrant_points=[_qdrant_point("69695", 43, 0.8123), _qdrant_point("69695", 1, 0.7001)],
    )
    async with Client(server) as client:
        res = await client.call_tool(
            "law_semantic_search", {"query": "合同解除的条件", "top_k": 2}
        )
    data = res.data
    assert embedder.texts == ["合同解除的条件"]
    assert data["count"] == 2
    top = data["results"][0]
    assert top["law_id"] == "69695" and top["chunk_no"] == 43
    assert top["score"] == 0.8123
    assert top["provenance"]["source"] == "law_db.law_chunks_v2"
    assert top["provenance"]["stable_id"] == "69695:43"
    assert top["provenance"]["as_of"] == _INGEST_TS.date().isoformat()
    assert top["publish"] == "2020-05-28 00:00:00"
    assert qdrant.calls[-1]["limit"] == 2


async def test_semantic_search_clamps_top_k(wired):
    server, _, qdrant = wired(qdrant_points=[_qdrant_point("1", 0, 0.5)])
    async with Client(server) as client:
        await client.call_tool("law_semantic_search", {"query": "x", "top_k": 999})
    assert qdrant.calls[-1]["limit"] == 50


async def test_semantic_search_empty_query_is_validation(wired):
    server, _, _ = wired()
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("law_semantic_search", {"query": "  "})
    assert "validation" in str(excinfo.value)


# ---------------------------------------------------------------- law_reference

async def test_reference_resolves_article_across_chunks(wired):
    chunks = _mini_chunks()  # 第一百四十三条 跨 chunk 1 -> 2
    server, _, _ = wired(
        laws_rows=[_law_row()],
        chunks_by_law={69695: chunks},
    )
    async with Client(server) as client:
        res = await client.call_tool("law_reference", {"citation": "《民法典》第一百四十三条"})
    data = res.data
    assert data["law"]["id"] == 69695
    assert data["law"]["provenance"]["source"] == "law_db.laws"
    assert data["article"]["no"] == 143
    assert data["article"]["display"] == "第一百四十三条"
    assert data["article"]["truncated"] is False
    excerpt = data["article"]["excerpt"]
    assert excerpt.startswith("第一百四十三条")
    assert "（二）意思表示真实" in excerpt  # 跨 chunk 续块拼进来了
    assert "第一百四十四条" not in excerpt  # 下一条不含在内
    assert data["chunk"]["chunk_id"] == "69695:1"
    assert data["chunk"]["provenance"]["stable_id"] == "69695:1"
    assert data["chunk"]["provenance"]["source"] == "law_db.law_chunks_v2"


async def test_reference_prefers_canonical_full_title(wired):
    chunks = _mini_chunks(law_id=18726)
    server, _, _ = wired(
        laws_rows=[
            _law_row(id=69782,
                     title="全国人民代表大会常务委员会关于《中华人民共和国刑法》第三十条的解释"),
            _law_row(id=18726, title="中华人民共和国刑法", category="重下载"),
        ],
        chunks_by_law={18726: chunks},
    )
    async with Client(server) as client:
        res = await client.call_tool("law_reference", {"citation": "刑法第二条"})
    assert res.data["law"]["id"] == 18726  # 全称惯用形 > contains 命中的立法解释


async def test_reference_without_article_locates_law_only(wired):
    server, _, _ = wired(laws_rows=[_law_row()])
    async with Client(server) as client:
        res = await client.call_tool("law_reference", {"citation": "《民法典》"})
    assert res.data["article"] is None
    assert "未含条号" in res.data["note"]
    assert res.data["law"]["id"] == 69695


async def test_reference_unknown_law_is_not_found(wired):
    server, _, _ = wired(laws_rows=[])
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("law_reference", {"citation": "《不存在的法典》第一条"})
    assert "not_found" in str(excinfo.value)
    assert "不存在的法典" in str(excinfo.value)


async def test_reference_unknown_article_is_not_found(wired):
    server, _, _ = wired(laws_rows=[_law_row()], chunks_by_law={69695: _mini_chunks()})
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("law_reference", {"citation": "《民法典》第九百九十九条"})
    assert "not_found" in str(excinfo.value)
    assert "第九百九十九条" in str(excinfo.value)


async def test_reference_law_without_chunks_is_not_found(wired):
    server, _, _ = wired(laws_rows=[_law_row()], chunks_by_law={})
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool("law_reference", {"citation": "《民法典》第一条"})
    assert "not_found" in str(excinfo.value)


async def test_reference_suffix_article(wired):
    chunks = _mini_chunks()
    chunks[1]["chunk_text"] = chunks[1]["chunk_text"].rstrip("\n") + "第十七条之一　已满七十五周岁的人故意犯罪的，可以从轻或者减轻处罚。\n"
    server, _, _ = wired(laws_rows=[_law_row()], chunks_by_law={69695: chunks})
    async with Client(server) as client:
        res = await client.call_tool("law_reference", {"citation": "《民法典》第十七条之一"})
    assert res.data["article"]["no"] == 17
    assert res.data["article"]["display"] == "第十七条之一"
    assert res.data["article"]["excerpt"].startswith("第十七条之一")


def test_not_found_and_validation_are_lex_errors():
    assert NotFound.kind == "not_found"
    assert Validation.kind == "validation"


# ---------------------------------------------------------------- law_read 分页

async def test_law_read_chunks_are_windowed_and_resumable(wired):
    """条文按窗口返回；分段拼接即全文，且 total_chunks 让调用方知道还有没有下一页。

    整部法规一次全量回传会让大法典独占调用方上下文（民法典 1260 条）。
    """
    _, _, _ = wired()
    rows = _mini_chunks()
    server, _, _ = wired(chunks_by_law={69695: rows}, laws_rows=[_law_row()])
    collected: list[str] = []
    offset = 0
    async with Client(server) as client:
        for _ in range(10):
            r = await client.call_tool(
                "law_read", {"law_id": 69695, "offset": offset, "limit": 1}
            )
            d = r.data
            assert d["total_chunks"] == 3
            assert d["chunk_offset"] == offset
            collected.extend(c["chunk_text"] for c in d["chunks"])
            if not d["truncated"]:
                assert "next_offset" not in d
                break
            offset = d["next_offset"]
        else:
            pytest.fail("条文分页未收敛")

    assert collected == [r["chunk_text"] for r in rows]


async def test_law_read_single_page_when_window_covers_all(wired):
    """窗口覆盖全部条文时一次返回，不出现截断标记。"""
    rows = _mini_chunks()
    server, _, _ = wired(chunks_by_law={69695: rows}, laws_rows=[_law_row()])
    async with Client(server) as client:
        r = await client.call_tool("law_read", {"law_id": 69695})
    d = r.data
    assert d["count"] == 3
    assert d["truncated"] is False
    assert "next_offset" not in d


async def test_law_read_offset_past_end_is_named(wired):
    """越过条文数的 offset 如实报错，不返回空列表冒充「法规到此为止」。"""
    from fastmcp.exceptions import ToolError

    rows = _mini_chunks()
    server, _, _ = wired(chunks_by_law={69695: rows}, laws_rows=[_law_row()])
    async with Client(server) as client:
        with pytest.raises(ToolError) as ei:
            await client.call_tool("law_read", {"law_id": 69695, "offset": 99})
    assert "99" in str(ei.value)
