"""骨架诚实性（残余占位）：仍处骨架阶段的工具以指名 tracking task 的错误失败——
不返回编造数据，也不静默返回空成功。

注意：law/case/guide 域在实现落地后由各自的测试文件接管行为验证；
本文件只保留长期占位（task 7.1 之前的 legal_docs 对）。
"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fd_legal_search_mcp.server import create_server

CALLS = [
    ("legal_docs_search", {"title_query": "监察法规"}),
    ("legal_docs_read", {"doc_id": 1}),
]


@pytest.fixture
def server():
    return create_server()


@pytest.mark.parametrize("name,args", CALLS)
async def test_stub_reports_its_tracking_task(server, name, args):
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(name, args)
    assert "lex-platform-v1 task" in str(excinfo.value)