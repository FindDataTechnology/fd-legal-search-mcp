#!/usr/bin/env python3
"""指导案例域 live 冒烟（lex-platform-v1 task 1.4 验收证据）。

用法：
    cd /Users/chengsishi/finddata/fd-legal-search-mcp
    set -a && . ./.env && set +a
    .venv/bin/python scripts/smoke_guide.py

流程：真实词检索（打印每条 provenance 三件套）→ 批次通知过滤 → 非法过滤值
拒绝 → 读取一篇（分节结构摘要）→ 未命中 case_id 如实 not_found。
只读；任何一步不符预期即非零退出。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if not os.environ.get("FDLEX_LAW_DATABASE_URL") and (REPO_ROOT / ".env").exists():
    from dotenv import dotenv_values

    for k, v in dotenv_values(REPO_ROOT / ".env").items():
        if v is not None:
            os.environ.setdefault(k, v)

from fastmcp import Client
from fastmcp.exceptions import ToolError

from fd_legal_search_mcp.server import create_server

_STEP = 0


def _step(title: str) -> None:
    global _STEP
    _STEP += 1
    print(f"\n=== {_STEP}. {title} ===")


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


async def main() -> None:
    server = create_server()
    async with Client(server) as client:
        _step('guide_cases_search 真实词 "买卖合同"（limit=5）')
        res = (await client.call_tool("guide_cases_search", {"title_query": "买卖合同", "limit": 5})).data
        print(f"count={res['count']} truncated={res['truncated']}")
        if not res["results"]:
            _fail("真实词零命中，不符合预期")
        for i, item in enumerate(res["results"]):
            p = item["provenance"]
            print(
                f"[{i}] {item['title'][:36]}…\n"
                f"    doc_type={item['doc_type']} reference_status={item['reference_status']} "
                f"publish={item['publish_at']}\n"
                f"    provenance: source={p['source']} stable_id={p['stable_id']} as_of={p['as_of']}"
            )
        if any(not all(item["provenance"].values()) for item in res["results"]):
            _fail("存在 provenance 三件套不完整的结果")

        _step('guide_cases_search 过滤 doc_type="notice"（批次通知）')
        res_n = (
            await client.call_tool("guide_cases_search", {"title_query": "指导性案例", "doc_type": "notice", "limit": 3})
        ).data
        print(f"count={res_n['count']}")
        for item in res_n["results"]:
            print(f"    [{item['source_id']}] {item['title'][:50]}… prov={item['provenance']}")

        _step('guide_cases_search 非法过滤 doc_type="判例" → 如实拒绝')
        try:
            await client.call_tool("guide_cases_search", {"title_query": "合同", "doc_type": "判例"})
        except ToolError as exc:
            print(f"ToolError（validation 语义）: {str(exc).split(': ', 1)[-1][:90]}")
        else:
            _fail("非法过滤值未被拒绝")

        _step("guide_cases_read 取检索首篇 → 分节结构摘要")
        case_id = res["results"][0]["provenance"]["stable_id"]
        doc = (await client.call_tool("guide_cases_read", {"case_id": case_id})).data
        sections = [k for k in ("keyword", "gist", "facts", "result", "reasoning", "statutes") if k in doc]
        print(f"case_id={case_id} title={doc['title'][:44]}…")
        print(f"reference_status={doc['reference_status']} reference_note={doc['reference_note']}")
        print(f"分节（未发布已省略）: {sections}")
        for k in sections:
            print(f"    {k}: {str(doc[k])[:48]}…（{len(str(doc[k]))} 字）")
        print(f"body: {len(doc['body'] or '')} 字；source_url={doc['source_url'][:60]}")
        print(f"provenance: {doc['provenance']}")
        if not all(doc["provenance"].values()):
            _fail("read 结果 provenance 三件套不完整")

        _step("guide_cases_read 未命中 case_id → 如实 not_found")
        try:
            await client.call_tool("guide_cases_read", {"case_id": "does-not-exist"})
        except ToolError as exc:
            print(f"ToolError（not_found 语义）: {str(exc).split(': ', 1)[-1][:80]}")
        else:
            _fail("未命中 id 未如实报错")

    print("\nSMOKE OK：指导案例域检索/读取/拒绝语义/provenance 三件套全部符合契约。")


if __name__ == "__main__":
    asyncio.run(main())
