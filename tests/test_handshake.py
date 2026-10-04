"""MCP 握手：骨架必须零配置启动，并暴露完整契约工具面（含描述）。"""

from __future__ import annotations

import pytest
from fastmcp import Client

from fd_legal_search_mcp.server import create_server

EXPECTED_TOOLS = {
    "law_search",
    "law_read",
    "law_semantic_search",
    "law_reference",
    "wenshu_search",
    "wenshu_read",
    "guide_cases_search",
    "guide_cases_read",
    "legal_docs_search",
    "legal_docs_read",
}


@pytest.fixture
def server():
    return create_server()


async def test_lists_full_contract_tool_surface(server):
    async with Client(server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_every_tool_has_a_description(server):
    async with Client(server) as client:
        for tool in await client.list_tools():
            assert tool.description and len(tool.description.strip()) > 40, tool.name