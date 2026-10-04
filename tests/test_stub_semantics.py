"""骨架诚实性：每个占位工具以指名 tracking task 的错误失败——
不返回编造数据，也不静默返回空成功。"""

from __future__ import annotations

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fd_legal_search_mcp.server import create_server

CALLS = [
    ("law_search", {"title_query": "民法典"}),
    ("law_read", {"law_id": 1}),
    ("law_semantic_search", {"query": "合同解除的条件"}),
    ("law_reference", {"citation": "《民法典》第一百四十三条"}),
    ("wenshu_search", {"title_query": "民间借贷"}),
    ("wenshu_read", {"doc_id": "x"}),
    ("guide_cases_search", {"title_query": "买卖合同"}),
    ("guide_cases_read", {"case_id": "x"}),
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