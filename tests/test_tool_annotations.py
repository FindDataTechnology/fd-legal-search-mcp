"""工具行为属性契约（mcp-tool-annotations）。

本服务 10 个工具全部只读——检索、读取、条级引用解析都只查既有语料，不写库、
不落缓存、不改索引。这条断言看着简单，但它是**自动批准策略的前提**：客户端
要敢不弹确认就调这 10 个工具，靠的正是"全都只读"这件事在协议层可见。

另有一条容易误判的属性：`openWorldHint` 全为假。判据是"结果是否取决于不可枚举
且会变化的外部集合"——LLM 与网页搜索是，本服务的封闭语料（law_db 三表 +
Qdrant law_chunks_v2 + RustFS 文书分片）不是：同一 stable_id 在同一语料版本上
恒得同一内容。连数据库不等于开放世界，所以这里刻意标假，让调用方可以假定结果
可复现。
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client

from fd_legal_search_mcp.annotations import TOOL_ANNOTATIONS, annotations_for
from fd_legal_search_mcp.server import create_server

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


@pytest.fixture(scope="module")
def runtime() -> dict[str, dict]:
    async def go():
        async with Client(create_server()) as client:
            out = {}
            for t in await client.list_tools():
                a = t.annotations
                assert a is not None, f"{t.name} 未声明行为属性"
                out[t.name] = {k: getattr(a, k) for k in HINTS}
            return out

    return asyncio.run(go())


def test_all_ten_tools_are_readonly(runtime):
    assert len(runtime) == 10
    for name, a in runtime.items():
        assert a["readOnlyHint"] is True, f"{name} 只读却未标只读"
        assert a["destructiveHint"] is False, f"{name} 无删除/改写接口，不应标破坏性"


def test_all_ten_tools_are_idempotent(runtime):
    for name, a in runtime.items():
        assert a["idempotentHint"] is True, f"{name} 只读查询应可安全重放"


def test_open_world_is_false_because_corpus_is_closed(runtime):
    """封闭语料 ≠ 开放世界：连数据库/向量库不改变这条判断。"""
    for name, a in runtime.items():
        assert a["openWorldHint"] is False, (
            f"{name}: 数据源是版本化封闭语料，标 openWorld 会让调用方误以为结果不可复现"
        )


def test_every_registered_tool_is_declared(runtime):
    assert set(runtime) == set(TOOL_ANNOTATIONS), (
        "运行时与登记表不一致：新增工具必须在 annotations.TOOL_ANNOTATIONS 登记"
    )


def test_unknown_tool_raises_instead_of_defaulting():
    """未登记即报错——沉默的默认值会让漏登记的工具蒙混过关。"""
    with pytest.raises(KeyError):
        annotations_for("no_such_tool")


def test_annotations_are_copies():
    a = annotations_for("law_search")
    a["readOnlyHint"] = False
    assert annotations_for("law_read")["readOnlyHint"] is True
