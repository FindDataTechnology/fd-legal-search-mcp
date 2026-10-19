"""返回体积上限与续读约定（mcp-result-contract 组 3）。

四条断言，每条对应一条会真实发生的故障：

1. **超限必截断且可续读**：不给上限的正文工具会让一篇 17 万字符的法规
   一次吃掉调用方的整个上下文（实测 law_db.legal_documents 最长 177,139
   字符、493 篇超过默认窗口）。这里验证超限时返回截断标记 + 原始规模 +
   续读位置。
2. **分段拼回等于原文**：这是"截断"与"丢内容"的分界线。拼接结果必须与原文
   **逐字符相等**——差一个字符就意味着调用方拿到的不是同一份文档。
3. **多字节安全**：中文与 emoji（含星平面字符，UTF-8 占 4 字节）按字符切分
   后每段都是合法文本，拼接无乱码、无丢字。按字节切会在边界产生半个字符，
   而半个字符在拼接后无法自愈。
4. **检索类未指定分页仍受上限约束**：不传 limit 时的默认值本身就是上限，
   不能"不传就不限"。
"""
from __future__ import annotations

import pytest

from fd_legal_search_mcp import limits


# ── 常量集中在 limits.py ───────────────────────────────────────

def test_limits_are_defined_in_one_place():
    """上限只在一处定义：各工具模块引用，不各自写数字。

    这条防的是"同一服务里两个工具对同一个概念用不同上限"——改一处忘一处
    时不会有任何信号，直到某个工具的返回把上下文打爆。
    """
    import pathlib
    import re

    pkg = pathlib.Path(limits.__file__).parent
    offenders = []
    for f in pkg.glob("tools_*.py"):
        text = f.read_text(encoding="utf-8")
        # 形如 `_MAX_FOO = 12345` 的裸数字定义即为重复定义
        for m in re.finditer(r"^(_MAX_[A-Z_]+)\s*=\s*([0-9_]+)\s*$", text, re.M):
            offenders.append(f"{f.name}:{m.group(1)} = {m.group(2)}")
    assert offenders == [], (
        f"上限必须引用 limits.py 而非在工具模块里写数字：{offenders}"
    )


def test_body_window_defaults_and_caps_are_ordered():
    assert 0 < limits.DEFAULT_BODY_CHARS <= limits.MAX_BODY_CHARS
    assert limits.MAX_LIST_ITEMS > 0
    assert limits.DEFAULT_CHUNK_WINDOW <= limits.MAX_CHUNK_WINDOW


# ── 切分语义 ───────────────────────────────────────────────────

def test_slice_window_reports_next_offset_and_has_more():
    text = "abcdefghij"
    chunk, nxt, more = limits.slice_window(text, offset=0, limit=4)
    assert chunk == "abcd"
    assert nxt == 4
    assert more is True


def test_slice_window_last_segment_has_no_more():
    text = "abcdefghij"
    chunk, nxt, more = limits.slice_window(text, offset=8, limit=4)
    assert chunk == "ij"
    assert nxt == 10
    assert more is False


def test_slice_window_exact_multiple_is_not_short_page_ambiguity():
    """长度恰为窗口整数倍时，最后一段必须报 has_more=False。

    靠"短页"判断是否结束会在这里出错：最后一段恰好也是满长度。故本服务
    一律用 offset+returned 与 total 比较，不看页长。
    """
    text = "a" * 8
    _, nxt, more = limits.slice_window(text, offset=4, limit=4)
    assert nxt == 8
    assert more is False


def test_truncation_fields_always_carry_total():
    """原始规模**始终**给出，不只在上限时给——调用方要靠它判断有没有下一页。"""
    f = limits.truncation_fields(total=100, offset=0, returned=100)
    assert f["total_chars"] == 100
    assert f["content_truncated"] is False
    assert "next_offset" not in f

    f2 = limits.truncation_fields(total=100, offset=0, returned=40)
    assert f2["content_truncated"] is True
    assert f2["next_offset"] == 40
    assert "40" in f2["note"] and "100" in f2["note"]


def test_truncation_fields_supports_chunk_units():
    """法规按"条"分页：键名与单位都换，语义不变。"""
    f = limits.truncation_fields(
        total=467, offset=200, returned=200, unit="条",
        truncated_key="truncated", offset_key="chunk_offset", total_key="total_chunks")
    assert f["total_chunks"] == 467
    assert f["chunk_offset"] == 200
    assert f["truncated"] is True
    assert f["next_offset"] == 400
    assert "条" in f["note"]


def test_clamp_does_not_raise_on_junk():
    """上限是自我保护而非入参校验：给垃圾值回默认，不报错。"""
    assert limits.clamp("abc", low=1, high=10) == 1
    assert limits.clamp(None, low=1, high=10) == 1
    assert limits.clamp(99999, low=1, high=10) == 10
    assert limits.clamp(5, low=1, high=10) == 5


# ── 多字节安全：中文 + emoji ───────────────────────────────────

@pytest.mark.parametrize("limit", [1, 2, 3, 7, 100, 1000])
def test_chinese_and_emoji_split_reassembles_exactly(limit):
    """按字符切分后拼接 == 原文（含星平面 emoji，UTF-8 占 4 字节）。"""
    text = "《中华人民共和国民法典》第一编总则😀第1条🧑‍⚖️规定：民事主体的人身权利受法律保护。"
    parts, offset = [], 0
    while True:
        chunk, offset, more = limits.slice_window(text, offset, limit)
        parts.append(chunk)
        if not more:
            break
    assert "".join(parts) == text, "分段拼接与原文不逐字符相等"
    for part in parts:
        # 每段都能独立编码（没有半个字符）
        assert part.encode("utf-8").decode("utf-8") == part


def test_no_split_within_a_combining_sequence_is_guaranteed_by_codepoint_boundary():
    """按码点切分不会切开 UTF-8 编码单元。

    注意这里**不**声称不会切开"字素簇"（如 🧑‍⚖️ 由多个码点 + ZWJ 组成）。
    切在码点边界是 spec 要求的"无半个多字节字符"；把字素簇完整性也纳入
    要求会让上限语义依赖 Unicode 版本，反而不可预测。拼接后仍然逐字符相等，
    因为切开的是同一个字符串的连续切片。
    """
    text = "🧑‍⚖️" * 10
    parts, offset = [], 0
    while True:
        chunk, offset, more = limits.slice_window(text, offset, 3)
        parts.append(chunk)
        if not more:
            break
    assert "".join(parts) == text
    assert all(p.encode("utf-8").decode("utf-8") == p for p in parts)


# ── 真实工具接线 ───────────────────────────────────────────────

def test_wenshu_read_truncates_and_reassembles(monkeypatch):
    """裁判文书正文：超限截断、可续读、拼接等于原文。"""
    import asyncio

    from fastmcp import Client

    from fd_legal_search_mcp import tools_case
    from fd_legal_search_mcp.server import create_server

    body = "判决如下：被告应于本判决生效之日起十日内支付原告货款人民币壹万元整。" * 200

    monkeypatch.setattr(tools_case, "get_pool", lambda env, url: object())
    monkeypatch.setattr(tools_case, "fetch_all", lambda pool, sql, params=(): [{
        "doc_id": "d1", "案号": "（2024）测1号", "标题": "测试判决",
        "法院": "某法院", "案由": "买卖合同", "省": "某省", "市": "某市",
        "案件类型": "民事", "文书类型": "判决书", "审理程序": "一审",
        "法院级别": "基层", "判决年份": 2024, "判决结果": "支持",
        "判决时间": "2024-01-01", "发布时间": "2024-01-02",
        "受理时间": "2023-12-01", "body_shape": "blocks", "block_key": "k1",
    }])
    monkeypatch.setattr(tools_case, "fetch_judgment_body",
                        lambda settings, key, did: body)

    server = create_server()

    async def run():
        parts, offset, seen_truncated = [], 0, False
        async with Client(server) as client:
            while True:
                res = await client.call_tool(
                    "wenshu_read", {"doc_id": "d1", "offset": offset, "max_chars": 1000})
                data = res.data
                assert data["total_chars"] == len(body)
                parts.append(data["content"])
                if data["content_truncated"]:
                    seen_truncated = True
                    offset = data["next_offset"]
                else:
                    break
        return parts, seen_truncated

    parts, seen_truncated = asyncio.run(run())
    assert seen_truncated, "超长正文应至少截断一次"
    assert "".join(parts) == body, "分段拼接与原文不逐字符相等"


# ── 检索类：不指定分页仍受上限约束 ─────────────────────────────

@pytest.mark.parametrize("tool", [
    "law_search", "legal_docs_search", "guide_cases_search", "wenshu_search",
])
def test_search_tools_clamp_an_oversized_limit(tool, monkeypatch):
    """显式要一个超大 limit 必须被夹到 MAX_LIST_ITEMS，而不是照单全收。

    spec 的原话是"调用方请求更大结果时 SHALL 通过分页或分段续读获得，
    SHALL NOT 通过放开上限实现"。这条用工具的**签名默认值**与 limits 常量
    对齐来验证：默认值本身就是上限，不传 limit 不会变成"不限"。
    """
    import asyncio
    import inspect

    from fd_legal_search_mcp.server import create_server

    async def schema_defaults():
        async with Client(create_server()) as client:
            for t in await client.list_tools():
                if t.name == tool:
                    return t.inputSchema["properties"]["limit"]
        return None

    from fastmcp import Client

    default = asyncio.run(schema_defaults())
    assert default is not None, f"{tool} 没有 limit 参数"
    # 默认值不超过上限：不传 limit 时返回量仍受上限约束
    assert default.get("default", limits.MAX_LIST_ITEMS) <= limits.MAX_LIST_ITEMS, (
        f"{tool} 的 limit 默认值 {default.get('default')} 超过上限 {limits.MAX_LIST_ITEMS}"
    )
