"""wenshu_search / wenshu_read 离线单测：monkeypatch db/RustFS，不依赖网络。

live（真实 wenshu_db + RustFS）验证只在 scripts/smoke_case.py 做；
本文件验证：排序语义、limit 截断、LIKE 转义、字典过滤未命中、provenance
三件套、双写方块解析（legacy / blocks.js）、正文缺失/不可读/截断的如实标注、
NotFound、签名与 docstring 与骨架逐字一致（task 7.1 对账基线）。
"""

from __future__ import annotations

import io
import json

import pytest
import zstandard
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fd_legal_search_mcp import tools_case
from fd_legal_search_mcp.server import create_server


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

def _install_no_db(monkeypatch):
    """任何 DB 访问都视为 bug。"""
    monkeypatch.setattr(tools_case, "get_pool",
                        lambda env, url: (_ for _ in ()).throw(
                            AssertionError(f"不应触库: {env}")))


def _install_search(monkeypatch, candidates, details=None, ref_rows=None,
                    candidate_sql=None):
    """patch get_pool/_fetch_candidates/fetch_all；返回捕获参数的 dict。"""
    captured: dict = {"candidate_sql": None, "candidate_params": None,
                      "fetch_all": []}

    def fake_candidates(pool, sql, params):
        captured["candidate_sql"] = sql
        captured["candidate_params"] = dict(params)
        if candidate_sql is not None:
            assert candidate_sql in sql, sql
        return list(candidates)

    def fake_fetch_all(pool, sql, params=()):
        captured["fetch_all"].append({"sql": sql, "params": params})
        if "FROM dict_texts" in sql:
            return list(ref_rows or [])
        return list(details or [])

    monkeypatch.setattr(tools_case, "get_pool", lambda env, url: object())
    monkeypatch.setattr(tools_case, "_fetch_candidates", fake_candidates)
    monkeypatch.setattr(tools_case, "fetch_all", fake_fetch_all)
    return captured


def _detail_row(doc_id, title, **over):
    row = {
        "doc_id": doc_id, "案号": f"（2021）京01民初1号-{doc_id}", "标题": title,
        "法院": "北京市第一中级人民法院", "案由": "民间借贷纠纷", "省": "北京",
        "市": None, "案件类型": "民事案件", "文书类型": "判决书",
        "审理程序": "一审", "法院级别": "中级法院", "判决年份": 2021,
        "判决结果": "部分支持", "判决时间": "2021-06-01",
        "发布时间": "2021-06-22", "受理时间": "2021-03-01",
        "has_body": True,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------
# 块构造（两种写方）
# --------------------------------------------------------------------------

def _legacy_block(payloads: dict[str, str]) -> bytes:
    """历史 Windows 写方：u32-BE 头长 + JSON{docs, offsets} + zstd payload。"""
    ids = list(payloads)
    encoded = {i: payloads[i].encode("utf-8") for i in ids}
    offsets, pos = [], 0
    for i in ids:
        offsets.append([pos, len(encoded[i])])
        pos += len(encoded[i])
    header = json.dumps({"docs": ids, "offsets": offsets, "raw": "x", "src": 1},
                        ensure_ascii=False).encode("utf-8")
    comp = zstandard.ZstdCompressor().compress(b"".join(encoded[i] for i in ids))
    return len(header).to_bytes(4, "big") + header + comp


def _blocks_js_block(payloads: dict[str, str]) -> bytes:
    """blocks.js 写方：整对象 zstd(magic + JSON{docs, offsets} + payload)。"""
    ids = list(payloads)
    encoded = [payloads[i].encode("utf-8") for i in ids]
    offsets, pos = [0], 0
    for chunk in encoded:
        pos += len(chunk)
        offsets.append(pos)
    header = json.dumps({"month": "2025-02", "docs": ids, "offsets": offsets},
                        ensure_ascii=False).encode("utf-8")
    body = (tools_case.BLOCK_MAGIC.encode("utf-8") + b"\n" + header + b"\n"
            + b"".join(encoded))
    return zstandard.ZstdCompressor().compress(body)


class _FakeBody:
    def __init__(self, blob: bytes):
        self._blob = blob

    def read(self) -> bytes:
        return self._blob


def _install_rustfs(monkeypatch, blob_or_exc):
    """patch get_rustfs；blob_or_exc 为字节（成功）或异常实例（GET 失败）。"""
    calls: dict = {}

    class FakeS3:
        def get_object(self, Bucket, Key):
            calls["bucket"], calls["key"] = Bucket, Key
            if isinstance(blob_or_exc, Exception):
                raise blob_or_exc
            return {"Body": _FakeBody(blob_or_exc)}

    monkeypatch.setattr(tools_case, "get_rustfs", lambda settings: FakeS3())
    return calls


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

async def test_search_ranks_exact_prefix_contains_with_provenance(monkeypatch):
    candidates = [
        {"doc_id": "c-contains", "标题": "关于民间借贷纠纷的会议纪要"},
        {"doc_id": "b-prefix", "标题": "民间借贷纠纷一审民事判决书 （2021）京01民初2号"},
        {"doc_id": "a-prefix", "标题": "民间借贷纠纷 （2021）京01民初1号"},
        {"doc_id": "0-exact", "标题": "民间借贷纠纷"},
    ]
    details = [
        _detail_row("0-exact", "民间借贷纠纷"),
        _detail_row("a-prefix", "民间借贷纠纷 （2021）京01民初1号"),
        _detail_row("b-prefix", "民间借贷纠纷一审民事判决书 （2021）京01民初2号"),
        _detail_row("c-contains", "关于民间借贷纠纷的会议纪要", 发布时间="2020-01-01"),
    ]
    _install_search(monkeypatch, candidates, details)
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_search",
                                        {"title_query": "民间借贷纠纷", "limit": 50})
    data = result.data
    titles = [r["标题"] for r in data["results"]]
    assert titles == ["民间借贷纠纷",
                      "民间借贷纠纷 （2021）京01民初1号",
                      "民间借贷纠纷一审民事判决书 （2021）京01民初2号",
                      "关于民间借贷纠纷的会议纪要"]
    assert data["count"] == 4 and data["truncated"] is False
    assert data["brand"] == "finddata"
    expected_as_of = {"0-exact": "2021-06-22", "a-prefix": "2021-06-22",
                      "b-prefix": "2021-06-22", "c-contains": "2020-01-01"}
    for item in data["results"]:
        assert item["provenance"]["source"] == "wenshu_db.judgments"
        assert item["provenance"]["stable_id"] == item["doc_id"]
        assert item["provenance"]["as_of"] == expected_as_of[item["doc_id"]]
        assert item["brand"] == "finddata"
        assert item["has_body"] is True
        # 内部坐标与原始字典 ref 绝不出现在结果里
        assert not ({"block_key", "block_ordinal", "src_id", "id_origin",
                     "法院引用", "案由引用"} & set(item))
    assert data["results"][0]["法院"] == "北京市第一中级人民法院"


async def test_search_empty_query_never_touches_db(monkeypatch):
    _install_no_db(monkeypatch)
    async with Client(create_server()) as client:
        for q in ["", "   "]:
            result = await client.call_tool("wenshu_search", {"title_query": q})
            assert result.data == {"results": [], "count": 0,
                                   "truncated": False, "brand": "finddata"}


async def test_search_limit_clamped_and_truncated_flag(monkeypatch):
    candidates = [{"doc_id": f"d{i}", "标题": f"民间借贷纠纷之{i:04d}"}
                  for i in range(250)]
    details = [_detail_row(f"d{i}", f"民间借贷纠纷之{i:04d}") for i in range(250)]
    _install_search(monkeypatch, candidates, details)
    async with Client(create_server()) as client:
        over = await client.call_tool("wenshu_search",
                                      {"title_query": "民间借贷纠纷", "limit": 999})
        under = await client.call_tool("wenshu_search",
                                       {"title_query": "民间借贷纠纷", "limit": 0})
        normal = await client.call_tool("wenshu_search",
                                        {"title_query": "民间借贷纠纷", "limit": 100})
    assert over.data["count"] == tools_case._MAX_LIMIT  # 200 上限
    assert over.data["truncated"] is True
    assert under.data["count"] == 1
    assert normal.data["count"] == 100
    assert normal.data["truncated"] is True  # 250 候选 > 100


async def test_search_candidate_cap_sets_note(monkeypatch):
    candidates = [{"doc_id": f"d{i}", "标题": f"民间借贷纠纷之{i:05d}"}
                  for i in range(tools_case._CANDIDATE_CAP)]
    _install_search(monkeypatch, candidates, [])
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_search",
                                        {"title_query": "民间借贷", "limit": 5})
    assert result.data["truncated"] is True
    assert str(tools_case._CANDIDATE_CAP) in result.data["note"]


async def test_search_escapes_like_metacharacters(monkeypatch):
    captured = _install_search(monkeypatch, [], [])
    async with Client(create_server()) as client:
        await client.call_tool("wenshu_search", {"title_query": r"民%间_借\贷"})
    params = captured["candidate_params"]
    assert params["pattern"] == r"%民\%间\_借\\贷%"
    assert params["cap"] == tools_case._CANDIDATE_CAP
    assert "ILIKE %(pattern)s" in captured["candidate_sql"]


async def test_search_dict_filter_miss_returns_empty_without_candidates(monkeypatch):
    captured = _install_search(monkeypatch, [], ref_rows=[])

    def no_candidates(pool, sql, params):
        raise AssertionError("字典未命中时不应查 judgments")

    monkeypatch.setattr(tools_case, "_fetch_candidates", no_candidates)
    async with Client(create_server()) as client:
        result = await client.call_tool(
            "wenshu_search", {"title_query": "民间借贷", "doc_type": "不存在的文书类型"})
    assert result.data["count"] == 0
    assert len(captured["fetch_all"]) == 1  # 只查了 dict_texts


async def test_search_upstream_failure_is_honest(monkeypatch):
    _install_search(monkeypatch, [], [])

    def boom(pool, sql, params):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(tools_case, "_fetch_candidates", boom)
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="wenshu_db 查询失败"):
            await client.call_tool("wenshu_search", {"title_query": "民间借贷"})


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------

def _read_row(**over):
    row = _detail_row("abc123", "民间借贷纠纷一审民事判决书 （2021）京01民初1号")
    row.update({"body_shape": "text", "block_key": "blocks/2025/02/000040.zst"})
    row.update(over)
    return row


_READ_SQL_MARK = 'WHERE j."doc_id" = %(doc_id)s'


def _install_read(monkeypatch, row):
    def fake_fetch_all(pool, sql, params=()):
        assert _READ_SQL_MARK in sql
        assert params == {"doc_id": "abc123"}
        return [dict(row)] if row is not None else []

    monkeypatch.setattr(tools_case, "get_pool", lambda env, url: object())
    monkeypatch.setattr(tools_case, "fetch_all", fake_fetch_all)


async def test_read_full_text_legacy_block(monkeypatch):
    payloads = {"abc123": "本院认为：本案系民间借贷纠纷。\n依照……判决如下："}
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, _legacy_block(payloads))
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    data = result.data
    assert data["content_available"] is True
    assert data["content"] == payloads["abc123"]
    assert data["body_shape"] == "text"
    assert data["brand"] == "finddata"
    assert data["provenance"] == {"source": "wenshu_db.judgments",
                                  "stable_id": "abc123", "as_of": "2021-06-22"}
    assert "block_key" not in data and "block_ordinal" not in data


async def test_read_full_text_blocks_js_block(monkeypatch):
    payloads = {"abc123": "blocks.js 写方正文。", "other": "别的篇"}
    _install_read(monkeypatch, _read_row(block_key="blocks/2025/01/000001.zst"))
    _install_rustfs(monkeypatch, _blocks_js_block(payloads))
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    assert result.data["content_available"] is True
    assert result.data["content"] == payloads["abc123"]


async def test_read_metadata_only_is_honest(monkeypatch):
    _install_read(monkeypatch, _read_row(body_shape="none", block_key=None))
    _install_rustfs(monkeypatch, AssertionError("无正文块时不应访问对象存储"))
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    data = result.data
    assert data["content_available"] is False
    assert data["note"] == tools_case._NO_BODY_NOTE
    assert "content" not in data
    assert data["provenance"]["stable_id"] == "abc123"


async def test_read_block_fetch_failure_notes_retry(monkeypatch):
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, RuntimeError("object store down"))
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    data = result.data
    assert data["content_available"] is False
    assert data["note"] == tools_case._BODY_UNAVAILABLE_NOTE
    assert "content" not in data


async def test_read_corrupt_block_is_fail_soft(monkeypatch):
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, b"\x00\x01not-a-block")
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    assert result.data["content_available"] is False


async def test_read_content_truncation_disclosed(monkeypatch):
    big = "正文" * 5000
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, _legacy_block({"abc123": big}))
    monkeypatch.setattr(tools_case, "_MAX_CONTENT_CHARS", 100)
    async with Client(create_server()) as client:
        result = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    data = result.data
    assert data["content_available"] is True
    assert data["content_truncated"] is True
    assert len(data["content"]) == 100
    assert "10000" in data["note"]


async def test_read_unknown_doc_id_is_not_found(monkeypatch):
    _install_read(monkeypatch, None)
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="不存在 doc_id=abc123"):
            await client.call_tool("wenshu_read", {"doc_id": "abc123"})


async def test_read_empty_doc_id_is_not_found(monkeypatch):
    _install_no_db(monkeypatch)
    async with Client(create_server()) as client:
        with pytest.raises(ToolError, match="doc_id 为空"):
            await client.call_tool("wenshu_read", {"doc_id": "  "})


def test_as_of_fallback_chain():
    assert tools_case._as_of({"发布时间": "2021-06-22"}) == "2021-06-22"
    assert tools_case._as_of({"发布时间": None, "判决时间": "2020-01-01"}) == "2020-01-01"
    assert tools_case._as_of({}) == "unknown"


def test_block_magic_constant():
    assert tools_case.BLOCK_MAGIC == "WENSHUBLOCK1"


# --------------------------------------------------------------------------
# 契约面：签名与 docstring 逐字对齐骨架（task 7.1 基线）
# --------------------------------------------------------------------------

async def test_tool_signatures_and_docstrings_preserved():
    async with Client(create_server()) as client:
        tools = {t.name: t for t in await client.list_tools()}

    def schema(tool) -> dict:
        return getattr(tool, "input_schema", None) or tool.inputSchema

    s = tools["wenshu_search"]
    props = set(schema(s)["properties"])
    assert props == {"title_query", "doc_type", "province",
                     "start_year", "end_year", "limit"}
    assert schema(s)["required"] == ["title_query"]
    assert schema(s)["properties"]["limit"].get("default") == 50
    desc = s.description or ""
    assert "ranked exact > prefix > contains" in desc
    assert "provenance（source=wenshu_db.judgments, stable_id=doc_id）" in desc
    assert "与 business-mcp wenshu_search 等价迁移（lex-platform-v1 task 7.1）" in desc
    # 口径以 fd-lex/collateral/numbers.md N3/N4 为准（3,700 万）。旧文案的
    # 1,050 万是历史快照，numbers.md 已明文废止该数字。
    assert "3,700万篇裁判文书" in desc

    r = tools["wenshu_read"]
    # 正文按窗口返回，故签名在 doc_id 之外带两个续读参数（默认值即文档窗口）。
    assert set(schema(r)["properties"]) == {"doc_id", "offset", "max_chars"}
    assert schema(r)["required"] == ["doc_id"]
    assert schema(r)["properties"]["offset"].get("default") == 0
    assert schema(r)["properties"]["max_chars"].get("default") == 12000
    rdesc = r.description or ""
    assert "Read one judgment's full text (zstd blocks on RustFS)." in rdesc
    assert "绝不编造正文" in rdesc
    assert "与 business-mcp wenshu_read 等价迁移（lex-platform-v1 task 7.1）" in rdesc
    assert "next_offset" in rdesc


def test_read_block_module_never_raises():
    """fetch_judgment_body 对一切坏输入返回 None，绝不抛。"""
    settings = type("S", (), {"rustfs_bucket": "wenshu"})()
    assert tools_case.fetch_judgment_body(settings, "", "x") is None
    assert tools_case.fetch_judgment_body(settings, "k", "") is None
    assert tools_case.fetch_judgment_body(settings, "k", "x") is None  # 无客户端配置


# --- 正文按窗口返回（体积上限与续读）-----------------------------------------


async def test_read_body_is_windowed_and_resumable(monkeypatch):
    """长正文分段返回；分段拼接必须与原文逐字符一致。

    单次响应把整篇正文塞回去会挤占调用方的上下文预算（旧上限 20 万字符 ≈
    20 万 token），所以正文按窗口返回，靠 next_offset 续读。
    """
    body = "本院认为：" + ("根据合同法相关规定，双方应当依约履行。\n" * 1500)
    assert len(body) > 20_000
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, _legacy_block({"abc123": body}))

    pieces: list[str] = []
    offset = 0
    async with Client(create_server()) as client:
        for _ in range(20):  # 有界循环：分段数必须收敛
            r = await client.call_tool(
                "wenshu_read", {"doc_id": "abc123", "offset": offset}
            )
            d = r.data
            assert len(d["content"]) <= d["total_chars"]
            assert d["content_offset"] == offset
            pieces.append(d["content"])
            if not d["content_truncated"]:
                assert "next_offset" not in d
                break
            assert d["next_offset"] == offset + len(d["content"])
            offset = d["next_offset"]
        else:
            pytest.fail("正文分段未在 20 次内收敛")

    assert "".join(pieces) == body, "分段拼接必须还原原文"


async def test_read_short_body_is_not_truncated(monkeypatch):
    """窗口内的正文一次返回完整，不出现截断标记。"""
    body = "短正文：判决如下。"
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, _legacy_block({"abc123": body}))
    async with Client(create_server()) as client:
        r = await client.call_tool("wenshu_read", {"doc_id": "abc123"})
    d = r.data
    assert d["content"] == body
    assert d["content_truncated"] is False
    assert d["total_chars"] == len(body)
    assert "next_offset" not in d


async def test_read_offset_past_end_is_named(monkeypatch):
    """越过末尾的 offset 如实报错，不返回空串冒充「正文到此为止」。"""
    from fastmcp.exceptions import ToolError

    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, _legacy_block({"abc123": "短正文"}))
    async with Client(create_server()) as client:
        with pytest.raises(ToolError) as ei:
            await client.call_tool(
                "wenshu_read", {"doc_id": "abc123", "offset": 9999}
            )
    assert "9999" in str(ei.value)


async def test_read_window_never_splits_a_cjk_character(monkeypatch):
    """按字符切分：每个分段都是合法文本，拼接无乱码。"""
    body = "中华人民共和国" * 4000
    _install_read(monkeypatch, _read_row())
    _install_rustfs(monkeypatch, _legacy_block({"abc123": body}))
    async with Client(create_server()) as client:
        r = await client.call_tool(
            "wenshu_read", {"doc_id": "abc123", "max_chars": 7}
        )
    d = r.data
    assert len(d["content"]) == 7
    assert d["content"] == body[:7]
    assert "\ufffd" not in d["content"]
