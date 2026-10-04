"""provenance 契约全工具覆盖（spec lex-mcp-surface，task 1.4）——live 门控。

仓库根 .env 存在时：经 fastmcp Client 以合理真实参数调用**每个**契约工具——
- 已接线：断言返回 results 每条携带完整 provenance 三件套
  （source / stable_id / as_of 全部非空）；
- 仍抛 not_wired（消息含「尚未接线」）：跳过并计数（并行任务实现期内是常态）；
- 其他错误：如实失败。
.env 不存在时整个模块 skip。设计为集成期可直接全量执行（task 1.4/1.2/1.3/7.1
全部落地后，此处零跳过全绿）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fd_legal_search_mcp.server import create_server

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"

pytestmark = pytest.mark.skipif(not ENV_FILE.exists(), reason="仓库根 .env 不存在，live 契约门跳过")

if ENV_FILE.exists():  # 加载真实坐标（不覆盖已设变量）
    try:
        from dotenv import dotenv_values

        _VALUES = dotenv_values(ENV_FILE)
    except ImportError:
        _VALUES = {}
        for _line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _VALUES[_k.strip()] = _v.strip()
    for _k, _v in _VALUES.items():
        if _v is not None:
            os.environ.setdefault(_k, _v)

#: 检索类工具 → 合理真实参数（真实库中必有命中的词）。
SEARCH_CALLS: dict[str, dict[str, Any]] = {
    "law_search": {"title_query": "民法典", "limit": 3},
    "wenshu_search": {"title_query": "买卖合同纠纷", "limit": 3},
    "guide_cases_search": {"title_query": "买卖合同", "limit": 3},
    "legal_docs_search": {"title_query": "监察法规", "limit": 3},
}

#: 读取类工具 → (喂 id 的检索工具, id 参数名)。id 取自检索结果的 provenance.stable_id
#: （契约本身承诺 stable_id 即来源库稳定标识）。
READ_SPECS: dict[str, tuple[str, str]] = {
    "law_read": ("law_search", "law_id"),
    "wenshu_read": ("wenshu_search", "doc_id"),
    "guide_cases_read": ("guide_cases_search", "case_id"),
    "legal_docs_read": ("legal_docs_search", "doc_id"),
}

#: 读取类工具的 int 型 id 参数（fastmcp 会做字符串→整数校验，需预转换）。
_INT_ID_TOOLS = {"law_read", "legal_docs_read"}

#: 未接线工具的跳过记录（工具名 → 原因），由 test_coverage_summary 汇报。
SKIPPED: dict[str, str] = {}

_TRIPLE = ("source", "stable_id", "as_of")


def _is_not_wired(exc: ToolError) -> bool:
    return "尚未接线" in str(exc)


def _assert_triple(prov: Any, where: str) -> None:
    assert isinstance(prov, dict), f"{where} 缺 provenance（非 dict）: {prov!r}"
    for key in _TRIPLE:
        assert prov.get(key), f"{where} provenance.{key} 缺失或为空: {prov!r}"


def _assert_search_payload(name: str, payload: Any) -> int:
    assert isinstance(payload, dict), f"{name} 返回非 dict: {type(payload)}"
    results = payload.get("results")
    assert isinstance(results, list), f"{name} 返回缺 results 列表: {payload!r}"
    for i, item in enumerate(results):
        _assert_triple(item.get("provenance") if isinstance(item, dict) else None, f"{name}.results[{i}]")
    return len(results)


def _assert_read_payload(name: str, payload: Any) -> None:
    assert isinstance(payload, dict), f"{name} 返回非 dict: {type(payload)}"
    if isinstance(payload.get("results"), list):  # 列表形态：逐条校验
        for i, item in enumerate(payload["results"]):
            _assert_triple(item.get("provenance") if isinstance(item, dict) else None, f"{name}.results[{i}]")
        return
    _assert_triple(payload.get("provenance"), name)  # 单体形态：payload 自带三件套


async def _search_or_skip(client: Client, name: str) -> list[dict[str, Any]] | None:
    """调用检索工具；未接线→记账并返回 None（由调用方决定 skip），其余异常如实抛出。"""
    try:
        payload = (await client.call_tool(name, SEARCH_CALLS[name])).data
    except ToolError as exc:
        if _is_not_wired(exc):
            SKIPPED[name] = "未接线"
            return None
        raise
    _assert_search_payload(name, payload)
    return payload["results"]


@pytest.mark.parametrize("name", list(SEARCH_CALLS))
async def test_search_tool_provenance_contract(name: str):
    async with Client(create_server()) as client:
        results = await _search_or_skip(client, name)
        if results is None:
            pytest.skip(f"{name} 尚未接线（骨架占位），provenance 契约暂跳")
        assert results, f"{name} 以真实词检索零命中，请核验参数 {SEARCH_CALLS[name]}"
        # provenance 三件套已在 _search_or_skip 内逐条断言


@pytest.mark.parametrize("name", list(READ_SPECS))
async def test_read_tool_provenance_contract(name: str):
    search_name, id_param = READ_SPECS[name]
    async with Client(create_server()) as client:
        results = await _search_or_skip(client, search_name)
        if results is None:
            SKIPPED[name] = f"{search_name} 未接线，读取侧无从取 id"
            pytest.skip(f"{search_name} 尚未接线（骨架占位），{name} provenance 契约暂跳")
        if not results:
            SKIPPED[name] = f"{search_name} 零命中，无法取 stable_id"
            pytest.skip(f"{search_name} 零命中，{name} 暂跳")
        stable_id = (results[0].get("provenance") or {}).get("stable_id")
        assert stable_id, f"{search_name} 首条缺 stable_id，无法驱动 {name}"
        args: dict[str, Any] = {id_param: stable_id}
        if name in _INT_ID_TOOLS:
            if not str(stable_id).isdigit():
                SKIPPED[name] = f"stable_id {stable_id!r} 非 int 型 id"
                pytest.skip(f"{name} 需 int id，stable_id={stable_id!r} 无法转换")
            args[id_param] = int(stable_id)
        try:
            payload = (await client.call_tool(name, args)).data
        except ToolError as exc:
            if _is_not_wired(exc):
                SKIPPED[name] = "未接线"
                pytest.skip(f"{name} 尚未接线（骨架占位），provenance 契约暂跳")
            raise
        _assert_read_payload(name, payload)


def test_coverage_summary():
    """契约覆盖面汇报：跳过只允许出现在并行实现期，集成期应为空。"""
    wired = [t for t in list(SEARCH_CALLS) + list(READ_SPECS) if t not in SKIPPED]
    print(f"\n[provenance 契约覆盖] 已验证 {len(wired)} 个工具: {sorted(wired)}")
    print(f"[provenance 契约覆盖] 跳过 {len(SKIPPED)} 个: {SKIPPED or '无'}")
