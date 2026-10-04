"""指导案例域单测（task 1.4）：ensure_provenance 契约守卫 + guide 域离线行为。

全部离线：monkeypatch 伪连接池，不依赖网络/数据库；live 行为由
tests/test_provenance_contract.py 与 scripts/smoke_guide.py 覆盖。
"""

from __future__ import annotations

import contextlib
from typing import Any, Callable, NamedTuple

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from fd_legal_search_mcp import tools_guide
from fd_legal_search_mcp.provenance import ensure_provenance
from fd_legal_search_mcp.server import create_server

# ---------------------------------------------------------------------------
# ensure_provenance 契约守卫
# ---------------------------------------------------------------------------


def _prov(source="s", stable_id="i", as_of="2020-01-01"):
    return {"source": source, "stable_id": stable_id, "as_of": as_of}


def _ok_item():
    return {"x": 1, "provenance": _prov()}


def test_ensure_provenance_missing_source_raises():
    with pytest.raises(AssertionError, match="三件套"):
        ensure_provenance([{"provenance": _prov(source="")}])


def test_ensure_provenance_missing_stable_id_raises():
    with pytest.raises(AssertionError, match="三件套"):
        ensure_provenance([{"provenance": _prov(stable_id="")}])


def test_ensure_provenance_missing_as_of_raises():
    with pytest.raises(AssertionError, match="三件套"):
        ensure_provenance([{"provenance": _prov(as_of="")}])


def test_ensure_provenance_missing_provenance_key_raises():
    with pytest.raises(AssertionError, match="三件套"):
        ensure_provenance([{"x": 1}])


def test_ensure_provenance_non_dict_item_raises():
    with pytest.raises(AssertionError, match="三件套"):
        ensure_provenance(["not-a-dict"])  # type: ignore[list-item]


def test_ensure_provenance_non_dict_provenance_raises():
    with pytest.raises(AssertionError, match="三件套"):
        ensure_provenance([{"provenance": "law_db"}])  # type: ignore[list-item]


def test_ensure_provenance_complete_items_pass_through():
    items = [_ok_item(), {"y": 2, "provenance": _prov("s2", "i2", "2021-12-31")}]
    assert ensure_provenance(items) is items


# ---------------------------------------------------------------------------
# guide 域离线单测：伪连接池（走真实 fetch_all 路径）+ fastmcp Client
# ---------------------------------------------------------------------------


class _Col(NamedTuple):
    name: str


class FakeCursor:
    """最小 psycopg 游标：记录 SQL/参数，回放 canned 行。"""

    def __init__(self, columns: list[str], rows: list[dict[str, Any]]):
        self._columns = columns
        self._rows = rows
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.executed.append((sql, tuple(params)))

    @property
    def description(self):
        return [_Col(c) for c in self._columns]

    def fetchall(self):
        return [tuple(r[c] for c in self._columns) for r in self._rows]


class FakeConn:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def cursor(self):
        return contextlib.nullcontext(self._cursor)


class FakePool:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def connection(self):
        return contextlib.nullcontext(FakeConn(self._cursor))


@pytest.fixture
def install_fake_pool(monkeypatch) -> Callable[[list[str], list[dict[str, Any]]], FakeCursor]:
    def _install(columns: list[str], rows: list[dict[str, Any]]) -> FakeCursor:
        cursor = FakeCursor(columns, rows)
        monkeypatch.setattr(tools_guide, "get_pool", lambda env_name, url: FakePool(cursor))
        return cursor

    return _install


@pytest.fixture
async def client():
    async with Client(create_server()) as c:
        yield c


_SEARCH_COLUMNS = [
    "source_id", "case_no", "doc_type", "title",
    "listed_at", "publish_at", "source", "reference_status", "rank",
]

_FAKE_SEARCH_ROWS = [
    {
        "source_id": "13306", "case_no": 9, "doc_type": "case",
        "title": "指导案例9号：上海存亮贸易有限公司诉蒋志东、王卫明等买卖合同纠纷案",
        "listed_at": "2012-09-26", "publish_at": "2012-09-26",
        "source": "最高人民法院", "reference_status": "不再参照", "rank": 0,
    },
    {
        "source_id": "4215", "case_no": 2, "doc_type": "case",
        "title": "指导案例2号：吴梅诉四川省眉山西城纸业有限公司买卖合同纠纷案",
        "listed_at": "2011-12-25", "publish_at": "2011-12-25",
        "source": "最高人民法院", "reference_status": "有效", "rank": 2,
    },
]


async def test_search_attaches_full_provenance_and_metadata(client, install_fake_pool):
    install_fake_pool(_SEARCH_COLUMNS, _FAKE_SEARCH_ROWS)
    data = (await client.call_tool("guide_cases_search", {"title_query": "买卖合同"})).data
    assert data["count"] == 2 and data["truncated"] is False
    assert len(data["results"]) == 2
    for item in data["results"]:
        assert item["provenance"]["source"] == "law_db.guide_cases"
        assert item["provenance"]["stable_id"] == item["source_id"]
        assert item["provenance"]["as_of"] == item["publish_at"]
        assert item["provenance"]["as_of"]  # 非空
    # 库端排序结果原样透出（rank 是 SQL 内部列，不外泄）
    assert [r["source_id"] for r in data["results"]] == ["13306", "4215"]
    # 爬虫记账列绝不外泄
    assert not ({"content_hash", "fetched_at", "loaded_at"} & set(data["results"][0]))


async def test_search_sql_carries_rank_ordering(client, install_fake_pool):
    cursor = install_fake_pool(_SEARCH_COLUMNS, list(reversed(_FAKE_SEARCH_ROWS)))  # 乱序入库
    data = (await client.call_tool("guide_cases_search", {"title_query": "买卖合同"})).data
    sql, _ = cursor.executed[0]
    # exact > prefix > contains 由库端 CASE rank + ORDER BY rank, title 保证
    assert "WHEN title ILIKE %s THEN 0" in sql
    assert "WHEN title ILIKE %s THEN 1" in sql
    assert "ORDER BY rank, title" in sql
    # 输出如实透传库端行序，不在服务端重排
    assert [r["source_id"] for r in data["results"]] == ["4215", "13306"]


async def test_search_threads_filters_and_limit(client, install_fake_pool):
    cursor = install_fake_pool(_SEARCH_COLUMNS, _FAKE_SEARCH_ROWS)
    data = (await client.call_tool(
        "guide_cases_search",
        {"title_query": "合同", "doc_type": "case", "reference_status": "有效", "limit": 5},
    )).data
    sql, params = cursor.executed[0]
    # 参数序：exact、prefix、pattern、doc_type、reference_status、limit+1
    assert params[0] == "合同"
    assert params[1] == "合同%"
    assert params[2] == "%合同%"
    assert params[3] == "case" and params[4] == "有效"
    assert params[5] == 6  # limit+1 探测截断
    assert "ORDER BY rank, title" in sql
    assert data["count"] == 2


async def test_search_escapes_like_metacharacters(client, install_fake_pool):
    cursor = install_fake_pool(_SEARCH_COLUMNS, [])
    data = (await client.call_tool("guide_cases_search", {"title_query": "100%_定金\\案"})).data
    _, params = cursor.executed[0]
    # % → \%、_ → \_、\ → \\（字面匹配，不放大通配）
    assert params[0] == "100\\%\\_定金\\\\案"
    assert params[1] == "100\\%\\_定金\\\\案%"
    assert params[2] == "%100\\%\\_定金\\\\案%"
    assert data == {"results": [], "count": 0, "truncated": False}


async def test_search_empty_query_is_honest_empty_result(client, install_fake_pool):
    cursor = install_fake_pool(_SEARCH_COLUMNS, _FAKE_SEARCH_ROWS)
    data = (await client.call_tool("guide_cases_search", {"title_query": "   "})).data
    assert data == {"results": [], "count": 0, "truncated": False}
    assert cursor.executed == []  # 未打库


@pytest.mark.parametrize(
    "field,value",
    [("doc_type", "判例"), ("doc_type", "CASE"), ("reference_status", "失效")],
)
async def test_search_rejects_closed_vocabulary_violations(client, install_fake_pool, field, value):
    cursor = install_fake_pool(_SEARCH_COLUMNS, _FAKE_SEARCH_ROWS)
    with pytest.raises(ToolError, match="validation"):
        await client.call_tool("guide_cases_search", {"title_query": "合同", field: value})
    assert cursor.executed == []  # 校验失败不触库


async def test_search_clamps_limit_both_ways(client, install_fake_pool):
    cursor = install_fake_pool(_SEARCH_COLUMNS, _FAKE_SEARCH_ROWS)
    await client.call_tool("guide_cases_search", {"title_query": "合同", "limit": 0})
    assert cursor.executed[0][1][-1] == 2  # 下限 1 → limit+1=2
    await client.call_tool("guide_cases_search", {"title_query": "合同", "limit": 99999})
    assert cursor.executed[1][1][-1] == tools_guide._MAX_LIMIT + 1


_READ_COLUMNS = [
    "source_id", "case_no", "doc_type", "title", "listed_at", "publish_at",
    "source", "source_url", "keyword", "gist", "facts", "result",
    "reasoning", "statutes", "body", "reference_status", "reference_note",
]

_FAKE_READ_ROW = {
    "source_id": "13306", "case_no": 9, "doc_type": "case",
    "title": "指导案例9号：上海存亮贸易有限公司诉蒋志东、王卫明等买卖合同纠纷案",
    "listed_at": "2012-09-26", "publish_at": "2012-09-26",
    "source": "最高人民法院", "source_url": "http://example.com/g9",
    "keyword": "民事 买卖合同", "gist": "关联公司人格混同", "facts": "……",
    "result": "……", "reasoning": None, "statutes": None,
    "body": "全文……", "reference_status": "不再参照", "reference_note": "法〔2020〕343号",
    "content_hash": "secret", "fetched_at": "2024-01-01", "loaded_at": "2024-01-01",
}


async def test_read_returns_sections_omitting_unpublished(client, install_fake_pool):
    install_fake_pool(_READ_COLUMNS, [_FAKE_READ_ROW])
    data = (await client.call_tool("guide_cases_read", {"case_id": "13306"})).data
    assert data["provenance"] == {
        "source": "law_db.guide_cases", "stable_id": "13306", "as_of": "2012-09-26",
    }
    assert data["body"] == "全文……"
    assert data["reference_status"] == "不再参照"
    assert data["reference_note"] == "法〔2020〕343号"
    # 已发布分节在，未发布分节（NULL）整个省略，不留 null 占位
    assert {"keyword", "gist", "facts", "result"} <= set(data)
    assert "reasoning" not in data and "statutes" not in data
    # 爬虫记账列绝不外泄
    assert not ({"content_hash", "fetched_at", "loaded_at"} & set(data))


async def test_read_passes_case_id_as_positional_param(client, install_fake_pool):
    cursor = install_fake_pool(_READ_COLUMNS, [_FAKE_READ_ROW])
    await client.call_tool("guide_cases_read", {"case_id": "13306"})
    assert cursor.executed[0][1] == ("13306",)


async def test_read_unknown_case_id_raises_not_found(client, install_fake_pool):
    cursor = install_fake_pool(_READ_COLUMNS, [])
    with pytest.raises(ToolError, match="not_found"):
        await client.call_tool("guide_cases_read", {"case_id": "999999"})
    assert cursor.executed  # 打了库、如实未命中
