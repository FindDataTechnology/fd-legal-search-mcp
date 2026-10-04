#!/usr/bin/env python3
"""scripts/smoke_case.py — lex-platform-v1 task 1.3 live smoke（只读，不写任何文件）。

验证 wenshu_search / wenshu_read 对真实 wenshu_db（37M 行）与 RustFS zstd 块的
端到端行为，产出验收证据：
  1. 真实检索词「民间借贷」检索（limit=100），打印前 5 条 doc_id/标题/案号/
     provenance 三件套，报告实际返回条数与截断标注；
  2. 取其中一篇 has_body 的做 wenshu_read 全文读取，报告正文字节数/字符数
     与头 80 字摘录；
  3. 负例：不存在的 doc_id → 如实 NotFound。

用法（仓库根）：
  set -a && . ./.env && set +a && .venv/bin/python scripts/smoke_case.py
（脚本也自带 .env 加载，直接 .venv/bin/python scripts/smoke_case.py 即可。）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    """轻量 .env 加载（不覆盖已存在变量）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        import os

        os.environ.setdefault(key, value)


def _excerpt(text: str, n: int = 80) -> str:
    return text[:n].replace("\r", " ").replace("\n", "⏎")


async def _main() -> int:
    _load_env()
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from fd_legal_search_mcp.server import create_server

    server = create_server()
    failures: list[str] = []

    async with Client(server) as client:
        # ---- 1. 真实检索词 --------------------------------------------
        print("=" * 72)
        print("[1] wenshu_search(title_query='民间借贷', limit=100) — live wenshu_db")
        t0 = time.time()
        try:
            res = (await client.call_tool(
                "wenshu_search",
                {"title_query": "民间借贷", "limit": 100},
            )).data
        except ToolError as exc:
            print(f"  FAIL: ToolError: {exc}")
            return 1
        elapsed = time.time() - t0
        results = res["results"]
        print(f"  耗时: {elapsed:.2f}s | count={res['count']} | "
              f"truncated={res['truncated']} | brand={res.get('brand')}")
        if res.get("note"):
            print(f"  note: {res['note']}")
        print(f"  实际抽样条数: {len(results)}（申请 100；"
              f"{'达标' if len(results) == 100 else '不足 100，如实说明：命中不足'}）")
        if not results:
            print("  FAIL: 真实检索词零命中")
            return 1
        for i, item in enumerate(results[:5]):
            prov = item["provenance"]
            print(f"  [{i}] doc_id={item['doc_id']}")
            print(f"      标题={_excerpt(item.get('标题') or '', 60)}")
            print(f"      案号={item.get('案号')} | 文书类型={item.get('文书类型')}"
                  f" | 判决年份={item.get('判决年份')} | has_body={item.get('has_body')}")
            print(f"      provenance: source={prov['source']} "
                  f"stable_id={prov['stable_id']} as_of={prov['as_of']}")

        # ---- 2. 全文读取 ----------------------------------------------
        print("=" * 72)
        print("[2] wenshu_read — 取检索结果中第一篇 has_body 的做全文读取")
        target = next((r for r in results if r.get("has_body")), results[0])
        t0 = time.time()
        read = (await client.call_tool("wenshu_read",
                                       {"doc_id": target["doc_id"]})).data
        elapsed = time.time() - t0
        print(f"  耗时: {elapsed:.2f}s | doc_id={target['doc_id']}")
        print(f"  标题={_excerpt(read.get('标题') or '', 60)}")
        print(f"  content_available={read.get('content_available')} "
              f"| body_shape={read.get('body_shape')}")
        if read.get("content_available"):
            content = read["content"]
            chars, bites = len(content), len(content.encode("utf-8"))
            print(f"  正文字符数: {chars} | 字节数(utf-8): {bites}"
                  f"{' | [已截断]' if read.get('content_truncated') else ''}")
            print(f"  头 80 字摘录: {_excerpt(content)}")
        else:
            print(f"  如实标注（无正文）: {read.get('note')}")
            failures.append("has_body 的第一篇读不到正文")
        print(f"  provenance: {json.dumps(read['provenance'], ensure_ascii=False)}")

        # ---- 3. 负例 ---------------------------------------------------
        print("=" * 72)
        print("[3] 负例: wenshu_read(doc_id='000000000000000000000000deadbeef')")
        try:
            await client.call_tool(
                "wenshu_read", {"doc_id": "000000000000000000000000deadbeef"})
            print("  FAIL: 不存在的 doc_id 未报 NotFound")
            failures.append("负例未如实 NotFound")
        except ToolError as exc:
            print(f"  如实 NotFound（ToolError）: {str(exc).strip()[:120]}")

    print("=" * 72)
    if failures:
        print("SMOKE FAIL:", "; ".join(failures))
        return 1
    print("SMOKE PASS: 检索 / 全文读取 / 负例 NotFound 三项证据齐备")
    return 0


if __name__ == "__main__":
    import anyio
    raise SystemExit(anyio.run(_main))
