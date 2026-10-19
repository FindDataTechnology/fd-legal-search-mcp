"""五源法律语料域两工具的离线行为测试（不触网：数据源一律 monkeypatch）。

覆盖：命中排序（exact > prefix > contains）、来源封闭词表拒绝、
正文未抓取时 content 键缺失、not_found、provenance 三件套齐备。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

import fd_legal_search_mcp.tools_corpus as tc
from fd_legal_search_mcp.config import Settings
from fd_legal_search_mcp.server import create_server

_LOADED = datetime(2026, 9, 25, 3, 12, 0, tzinfo=timezone.utc)


def _settings() -> Settings:
    return Settings(
        law_database_url="postgresql://fake/law_db",
        wenshu_database_url=None,
        qdrant_url=None,
        qdrant_collection="law_chunks_v2",
        qdrant_api_key=None,
        rustfs_endpoint=None,
        rustfs_bucket="wenshu",
        rustfs_access_key=None,
        rustfs_secret_key=None,
        embed_backend="api",
        embed_api_url=None,
        embed_api_key=None,
        embed_model="",
        embed_dim=1024,
    )


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "id": 501,
        "title": "中华人民共和国安全生产法",
        "source": "gov-rules-library",
        "doc_type": "行政法规",
        "status": "现行有效",
        "authority": "国务院",
        "language": "zh",
        "published_at": "2021-06-10",
        "effective_at": "2021-09-01",
        "has_content": True,
        "loaded_at": _LOADED,
    }
    row.update(over)
    return row


def _wire(monkeypatch, rows_by_sql: dict[str, list[dict]] | list[dict]) -> None:
    """Route fetch_all by a substring marker in the SQL."""
    def route(pool, sql, params=()):
        if isinstance(rows_by_sql, list):
            return rows_by_sql
        for marker, rows in rows_by_sql.items():
            if marker in sql:
                return rows
        return []
    monkeypatch.setattr(tc, "get_pool", lambda env, url: object())
    monkeypatch.setattr(tc, "fetch_all", route)


async def _call(tool: str, **kwargs):
    async with Client(create_server(_settings())) as c:
        return (await c.call_tool(tool, kwargs)).structured_content


@pytest.mark.asyncio
async def test_search_ranks_exact_prefix_contains(monkeypatch):
    rows = [
        _row(id=1, title="安全生产法实施条例"),
        _row(id=2, title="安全生产法"),
        _row(id=3, title="某地安全生产法释义"),
    ]
    _wire(monkeypatch, rows)
    out = await _call("legal_docs_search", title_query="安全生产法")
    assert [r["id"] for r in out["results"]] == [2, 1, 3]
    assert out["count"] == 3
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_search_rejects_unknown_source(monkeypatch):
    _wire(monkeypatch, [])
    out = await _call("legal_docs_search", title_query="条例", source="not-a-corpus")
    assert out["status"] == "invalid_filter"
    assert out["field"] == "source"
    assert "gov-rules-library" in out["valid_sources"]


@pytest.mark.asyncio
async def test_search_blank_query_matches_nothing(monkeypatch):
    _wire(monkeypatch, [_row()])
    out = await _call("legal_docs_search", title_query="   ")
    assert out["results"] == []
    assert out["count"] == 0


@pytest.mark.asyncio
async def test_search_hits_carry_provenance(monkeypatch):
    _wire(monkeypatch, [_row()])
    out = await _call("legal_docs_search", title_query="安全生产法")
    prov = out["results"][0]["provenance"]
    assert prov["source"] == "law_db.legal_documents"
    assert prov["stable_id"] == "501"
    assert prov["as_of"].startswith("2026-09-25")


@pytest.mark.asyncio
async def test_search_truncates_and_reports(monkeypatch):
    rows = [_row(id=i, title=f"安全生产法第{i}号") for i in range(5)]
    _wire(monkeypatch, rows)
    out = await _call("legal_docs_search", title_query="安全生产法", limit=2)
    assert out["count"] == 2
    assert out["truncated"] is True


@pytest.mark.asyncio
async def test_read_returns_body_when_available(monkeypatch):
    _wire(monkeypatch, [{
        **_row(), "source_url": "https://example.gov.cn/a",
        "metadata": {"object_keys": ["x"], "pages": 3},
        "content_available": True, "content": "第一条　为了……",
    }])
    out = await _call("legal_docs_read", doc_id=501)
    assert out["content"] == "第一条　为了……"
    assert out["content_available"] is True
    assert "note" not in out
    # 爬虫内部字段必须被剥掉
    assert out["metadata"] == {"pages": 3}
    assert out["provenance"]["stable_id"] == "501"


@pytest.mark.asyncio
async def test_read_omits_content_key_when_body_missing(monkeypatch):
    _wire(monkeypatch, [{
        **_row(), "source_url": None, "metadata": {},
        "content_available": False, "content": None,
    }])
    out = await _call("legal_docs_read", doc_id=501)
    assert "content" not in out
    assert out["content_available"] is False
    assert out["note"]


@pytest.mark.asyncio
async def test_read_unknown_id_is_not_found(monkeypatch):
    """未找到走错误码（与 law_read / guide_cases_read 一致）。

    曾经返回一个成功的 `{"status": "not_found"}` 载荷，另两个 read 工具却抛
    NotFound——同一服务对同一件事两种形状，调用方得写两条分支。
    """
    _wire(monkeypatch, [])
    with pytest.raises(ToolError) as exc:
        await _call("legal_docs_read", doc_id=999999)
    assert "not_found" in str(exc.value)
