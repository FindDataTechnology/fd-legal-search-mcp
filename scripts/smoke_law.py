#!/usr/bin/env python3
"""法条域 live 冒烟（lex-platform-v1 task 1.2 验收）。

对真实数据源（law_db / Qdrant law_chunks_v2 / SiliconFlow embedding）逐项验收：
1. 条级引用解析：《民法典》第一百四十三条 /《刑法》第二十条 /《公司法》第三条
   三条真实解析成功（打印 law_id / 条号 / chunk 摘录 / provenance），
   外加一条「查无此法」负例，如实 not_found；
2. 语义检索：自然语言查询返回 5-10 条带完整 provenance 的 chunk；
3. law_search / law_read 各一条真实输出样本。

跑法（本机 Tailnet 直连）：
  cd fd-legal-search-mcp && set -a && . ./.env && set +a && .venv/bin/python scripts/smoke_law.py

只读操作，不写任何数据源。任一断言失败以非零码退出。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

if (REPO_ROOT / ".env").exists():
    for line in (REPO_ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from fd_legal_search_mcp.config import Settings  # noqa: E402
from fd_legal_search_mcp.server import create_server  # noqa: E402


def _prov(item: dict) -> str:
    p = item.get("provenance") or {}
    return f"source={p.get('source')} stable_id={p.get('stable_id')} as_of={p.get('as_of')}"


def _head(text: str | None, n: int = 90) -> str:
    return (text or "").replace("\n", " ")[:n]


async def main() -> int:
    server = create_server(Settings.from_env())
    failures: list[str] = []

    def check(cond: bool, what: str) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {what}")
        if not cond:
            failures.append(what)

    async with Client(server) as client:
        # ---- 1) 条级引用解析：三条真实解析 + 一条如实 not_found -----------------
        print("\n== law_reference 条级引用解析（live）==")
        for citation, expect_law_id, expect_no in [
            ("《民法典》第一百四十三条", 69695, 143),
            ("《刑法》第二十条", 18726, 20),
            ("《公司法》第三条", 18708, 3),
        ]:
            print(f"\n-- {citation}")
            res = await client.call_tool("law_reference", {"citation": citation})
            data = res.data
            law, art, chunk = data["law"], data["article"], data["chunk"]
            print(f"  law: id={law['id']} title={law['title']!r} category={law['category']!r} "
                  f"status={law['status']!r} publish={law['publish']!r}")
            print(f"  law provenance: {_prov(law)}")
            print(f"  条号: 第{art['no']}条 (display={art['display']!r}) truncated={art['truncated']}")
            print(f"  chunk: chunk_id={chunk['chunk_id']} chunk_no={chunk['chunk_no']} "
                  f"char_start={chunk['char_start']} char_end={chunk['char_end']}")
            print(f"  chunk provenance: {_prov(chunk)}")
            print(f"  摘录: {_head(art['excerpt'], 120)}")
            check(law["id"] == expect_law_id, f"{citation} -> law_id={expect_law_id}")
            check(art["no"] == expect_no, f"{citation} -> 条号 {expect_no}")
            check(bool(art["excerpt"].strip()), f"{citation} 摘录非空")
            check(all(chunk["provenance"].get(k) for k in ("source", "stable_id", "as_of")),
                  f"{citation} chunk provenance 三件套完整")

        print("\n-- 负例：《不存在的法典》第一条（应如实 not_found）")
        try:
            await client.call_tool("law_reference", {"citation": "《不存在的法典》第一条"})
            check(False, "负例应抛 not_found，却成功返回")
        except ToolError as exc:
            print(f"  ToolError: {exc}")
            check("not_found" in str(exc), "负例如实返回 not_found")

        # ---- 2) 语义检索 ------------------------------------------------------
        print("\n== law_semantic_search 语义检索（live）==")
        res = await client.call_tool(
            "law_semantic_search", {"query": "合同解除的条件", "top_k": 8}
        )
        data = res.data
        print(f"  query={data['query']!r} count={data['count']}")
        for i, item in enumerate(data["results"], 1):
            print(f"  [{i}] score={item['score']:.4f} {item['title']!r} 第{item['chunk_no']}块 "
                  f"law_id={item['law_id']} stable_id={item['provenance']['stable_id']} "
                  f"as_of={item['provenance']['as_of']}")
            print(f"      {_head(item['chunk_text'], 72)}")
        check(5 <= data["count"] <= 10, "语义检索返回 5-10 条")
        check(all(all(item["provenance"].get(k) for k in ("source", "stable_id", "as_of"))
                  for item in data["results"]), "逐条 provenance 三件套完整")

        # ---- 3) law_search / law_read 样本 ------------------------------------
        # 注：截断发生在 DB 排序后（business-mcp 等价语义），小 limit 下
        # 「首条即目标」不成立；样本用默认 limit（全量候选回表后 python 精确排序）。
        print("\n== law_search 检索样本（live）==")
        res = await client.call_tool("law_search", {"title_query": "民法典"})
        data = res.data
        print(f"  count={data['count']} truncated={data['truncated']}")
        for item in data["results"][:3]:
            print(f"  - id={item['id']} {item['title']!r} category={item['category']!r} "
                  f"status={item['status']!r} | {_prov(item)}")
        check(data["count"] >= 1 and data["results"][0]["title"] == "中华人民共和国民法典",
              "law_search(民法典) 首条即《民法典》")
        check(all(all(i["provenance"].get(k) for k in ("source", "stable_id", "as_of"))
                  for i in data["results"]), "law_search 逐条 provenance 完整")
        res3 = await client.call_tool("law_search", {"title_query": "民法典", "limit": 3})
        check(res3.data["count"] == 3 and res3.data["truncated"] is True,
              "law_search limit=3 如实截断（count=3, truncated=True）")

        law_id = data["results"][0]["provenance"]["stable_id"]
        print("\n== law_read 读取样本（live）==")
        res = await client.call_tool("law_read", {"law_id": int(law_id)})
        data = res.data
        print(f"  law: id={data['law']['id']} {data['law']['title']!r} | {_prov(data['law'])}")
        print(f"  chunks: count={data['count']}")
        first = data["chunks"][0]
        print(f"  chunk[0]: chunk_id={first['chunk_id']} char=[{first['char_start']},"
              f"{first['char_end']}) | {_prov(first)}")
        print(f"  chunk[0] 正文: {_head(first['chunk_text'], 120)}")
        check(data["law"]["id"] == int(law_id) and data["count"] >= 1,
              f"law_read({law_id}) 返回法规与 chunk 结构")
        check(all(all(c["provenance"].get(k) for k in ("source", "stable_id", "as_of"))
                  for c in data["chunks"]), "law_read chunk 逐条 provenance 完整")

    print("\n" + "=" * 64)
    if failures:
        print(f"SMOKE FAILED：{len(failures)} 项未过 -> {failures}")
        return 1
    print("SMOKE PASSED：条级引用解析(3 正例+1 负例) / 语义检索 / law_search / law_read 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
