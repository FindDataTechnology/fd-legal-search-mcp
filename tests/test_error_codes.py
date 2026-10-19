"""错误码契约（mcp-error-semantics 组 4）。

本服务的领域异常早已是三分类，但它们只是普通异常：fastmcp 会把它们包成
一段文本，`structuredContent` 为空——调用方只能读字符串判断类别。包一层后
三类的码与 `isError` 同时到达。

另有一条**一致性**断言：三个 read 工具对"未找到"必须给出同一种形状。
曾经 `legal_docs_read` 返回成功的 `{"status": "not_found"}` 载荷，而
`law_read` / `guide_cases_read` 抛 `NotFound`——同一服务对同一件事两种形状，
调用方得写两条分支，而那正是错误码标准化要消除的成本。
"""
from __future__ import annotations

import pathlib

import pytest

from fd_legal_search_mcp import error_codes as E
from fd_legal_search_mcp.error_codes import CODES, RETRYABLE, error_body
from fd_legal_search_mcp.error_mapping import _upstream_name, map_errors
from fd_legal_search_mcp.errors import NotFound, UpstreamUnavailable, Validation

REPO = pathlib.Path(__file__).resolve().parents[1]


# ── 码表 ───────────────────────────────────────────────────────

def test_codes_cover_the_four_classes():
    for required in (E.INVALID_INPUT, E.NOT_FOUND, E.NOT_WIRED, E.UPSTREAM_UNAVAILABLE):
        assert required in CODES


def test_only_upstream_is_retryable():
    assert RETRYABLE[E.UPSTREAM_UNAVAILABLE] is True
    for code in (E.INVALID_INPUT, E.NOT_FOUND, E.NOT_WIRED, E.INTERNAL_ERROR):
        assert RETRYABLE[code] is False


def test_unregistered_code_rejected():
    with pytest.raises(ValueError):
        error_body("invented_code", "x")


# ── 三类领域异常各有一个样例 ─────────────────────────────────

def test_validation_maps_to_invalid_input():
    @map_errors
    def tool():
        raise Validation("validation：query 为空，语义检索需要非空查询文本")

    err = tool().structured_content["error"]
    assert err["code"] == E.INVALID_INPUT
    assert err["retryable"] is False


def test_not_found_maps_to_not_found():
    @map_errors
    def tool():
        raise NotFound("not_found：law_id=1 不存在")

    err = tool().structured_content["error"]
    assert err["code"] == E.NOT_FOUND
    assert err["retryable"] is False


def test_upstream_unavailable_maps_and_names_the_upstream():
    @map_errors
    def tool():
        raise UpstreamUnavailable("未配置数据源：缺少环境变量 FDLEX_LAW_DATABASE_URL")

    err = tool().structured_content["error"]
    assert err["code"] == E.UPSTREAM_UNAVAILABLE
    assert err["retryable"] is True
    # 消息里写的是环境变量名，不含 "law_db"——只认库名会让最常见的
    # "没配"场景落到 unknown_upstream。
    assert err["upstream"] == "law_db"


@pytest.mark.parametrize("message,expected", [
    ("wenshu_db 查询失败：OperationalError", "wenshu_db"),
    ("缺少环境变量 FDLEX_WENSHU_DATABASE_URL", "wenshu_db"),
    ("缺少环境变量 FDLEX_LAW_DATABASE_URL", "law_db"),
    ("qdrant 连接被拒", "qdrant"),
    ("embed 网关超时", "embedder"),
    ("rustfs 不可达", "rustfs"),
    ("完全无关的错误", "unknown_upstream"),
])
def test_upstream_name_detection(message, expected):
    assert _upstream_name(Exception(message)) == expected


def test_unknown_exception_re_raised():
    @map_errors
    def tool():
        raise KeyError("real bug")

    with pytest.raises(KeyError):
        tool()


# ── 真实 MCP 往返 ─────────────────────────────────────────────

def _call(name: str, args: dict):
    import asyncio

    from fastmcp import Client

    from fd_legal_search_mcp.server import create_server

    async def go():
        async with Client(create_server()) as c:
            return await c.call_tool(name, args, raise_on_error=False)

    return asyncio.run(go())


def test_missing_datasource_is_upstream_unavailable(monkeypatch):
    """没配数据源时报 upstream_unavailable，而不是空结果。

    "取不到"与"真的没有"必须可区分——spec 的"不可静默降级为成功"。
    """
    for k in ("FDLEX_LAW_DATABASE_URL", "FDLEX_WENSHU_DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    r = _call("law_reference", {"citation": "《民法典》第一百四十三条"})
    assert r.is_error is True
    err = r.structured_content["error"]
    assert err["code"] == E.UPSTREAM_UNAVAILABLE
    assert err["upstream"] == "law_db"


def test_empty_query_is_invalid_input():
    r = _call("law_semantic_search", {"query": "   "})
    assert r.is_error is True
    assert r.structured_content["error"]["code"] == E.INVALID_INPUT


def test_read_tools_agree_on_not_found_shape():
    """三个 read 工具对"未找到"必须是同一种形状。

    这条针对的是一处真实的不一致：`legal_docs_read` 曾返回成功的
    `{"status": "not_found"}`，另两个抛 `NotFound`。同一服务两种形状，
    调用方得写两条分支——而两条分支里通常只有一条会被测到。
    """
    import inspect

    from fd_legal_search_mcp import tools_case, tools_corpus, tools_guide

    for mod in (tools_case, tools_corpus, tools_guide):
        src = inspect.getsource(mod)
        assert 'return {"status": "not_found"' not in src, (
            f"{mod.__name__} 仍以成功载荷表达未找到"
        )


def test_a_successful_empty_search_is_not_an_error():
    """有效但无匹配是正常空结果，不是错误（spec 明确区分这两者）。"""
    r = _call("law_search", {"title_query": "   "})
    assert r.is_error is False
    assert r.data == {"results": [], "count": 0, "truncated": False}


# ── 镜像一致性 ────────────────────────────────────────────────

def test_error_codes_mirror_the_quant_copy():
    src = REPO.parent / "fd-quant" / "fd_quant" / "errors.py"
    if not src.exists():
        pytest.skip("fd-quant 不在本工作区")

    def code_part(path):
        text = path.read_text(encoding="utf-8")
        end = text.find('"""', 3)
        return text[end + 3:].lstrip("\n")

    assert code_part(src) == code_part(REPO / "fd_legal_search_mcp" / "error_codes.py")
