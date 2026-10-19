"""工作流级知识随服务暴露（mcp-surface-contract 组 7）。

两条要求：

1. **资源可枚举可读**（7.1）：客户端 `list_resources` 能列出、
   `read_resource` 能读到，且内容是**多步组合指导**——判据是含多个编号步骤，
   且点名多个真实存在的工具。
2. **工具描述不含编排内容**（7.2）：编排知识搬到资源里。若两边都有就会各自
   漂移，而描述会被**每个**调用方读进上下文，编排段落的代价按调用次数线性放大。
"""
from __future__ import annotations

import asyncio
import re

import pytest
from fastmcp import Client

from fd_legal_search_mcp import workflows
from fd_legal_search_mcp.server import create_server

_TOOLS: set[str] | None = None


def tool_names() -> set[str]:
    global _TOOLS
    if _TOOLS is None:
        async def go():
            async with Client(create_server()) as c:
                return {t.name for t in await c.list_tools()}
        _TOOLS = asyncio.run(go())
    return _TOOLS


# ── 7.1 资源可枚举可读，且是多步组合 ──────────────────────────

def test_resources_are_enumerable_and_readable():
    async def go():
        async with Client(create_server()) as c:
            out = {}
            for r in await c.list_resources():
                got = await c.read_resource(r.uri)
                out[str(r.uri)] = "".join(getattr(x, "text", "") for x in got)
            return out

    texts = asyncio.run(go())
    assert set(texts) == set(workflows.RESOURCES)
    for uri, text in texts.items():
        assert len(text) > 400, f"{uri} 内容过短，不像工作流指导"


def test_resource_content_is_multi_step_and_names_real_tools():
    for uri, (_title, text) in workflows.RESOURCES.items():
        steps = re.findall(r"^(\d)\. \*\*", text, re.M)
        assert len(steps) >= 2, f"{uri} 不像多步指导（步骤 {len(steps)}）"
        # 两种写法都认：`wenshu_search(...)` 与 `wenshu_search`
        named = {t for t in tool_names()
                 if re.search(rf"`{re.escape(t)}`", text)
                 or re.search(rf"\b{re.escape(t)}\(", text)}
        assert len(named) >= 2, f"{uri} 只点名了 {named or '没有'}，不是组合指导"


def test_named_tools_actually_exist():
    """资源里点名的工具必须真在工具面上——否则指导会指向不存在的工具。"""
    # 只核查"形状像工具调用"的串：要么带 `(`，要么是已知动词结尾的复合名。
    # `guide_cases` 这类集合/表名不带动词后缀，不该被当成工具名。
    tool_like = re.compile(r"`([a-z_]+)\(`|`([a-z_]+_(?:search|read|reference))`")
    for uri, (_title, text) in workflows.RESOURCES.items():
        mentioned = {a or b for a, b in tool_like.findall(text)}
        bad = mentioned - tool_names()
        assert not bad, f"{uri} 点名了不存在的工具：{sorted(bad)}"


def test_resource_uris_are_namespaced_by_service():
    """URI 是全局命名空间：不前缀服务名会与其它服务的资源撞名。"""
    for uri in workflows.RESOURCES:
        assert uri.startswith("fd-legal-search-mcp://"), uri


# ── 7.2 工具描述不含工作流编排 ────────────────────────────────

ORCHESTRATION = re.compile(r"(下一步|接下来|然后调用|接着调用|调用顺序|依次调用|工作流)")


def test_no_tool_description_contains_orchestration():
    async def go():
        async with Client(create_server()) as c:
            return {t.name: (t.description or "") for t in await c.list_tools()}

    descs = asyncio.run(go())
    offenders = {n: d for n, d in descs.items() if ORCHESTRATION.search(d)}
    assert not offenders, f"这些描述含编排内容，应移到资源里：{sorted(offenders)}"
