#!/usr/bin/env python3
"""检索评测跑分器：金集逐条调检索函数，产出 nDCG@10 / hit@5 / MRR 分域分解。

- 直调 tools_* 内部函数（不起 MCP 服务，db 池经 get_pool 复用）
- 公式口径对齐 law-bench packs/text-retrieval（二元相关判定：命中期望 stable_id 集即相关）
- --baseline eval/baseline.json：对照并按 5% 相对降幅门（core=nDCG@10 分域）给 exit code
- 结果写 --out（默认 eval/latest.json），供 baseline 演进 diff
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden" / "legal-retrieval.jsonl"
REL_DROP_GATE = 0.05  # 5% 相对降幅门（spec：核心指标回归门）


def REL_DROP_KEY() -> str:
    return f"{REL_DROP_GATE:.0%}"

sys.path.insert(0, str(ROOT))


def _dcg(rels: list[int]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels))


def _ndcg_at_k(ranked_ids: list[str], expected: set[str], k: int = 10) -> float:
    rels = [1 if rid in expected else 0 for rid in ranked_ids[:k]]
    ideal = sorted(rels, reverse=True)
    d = _dcg(ideal)
    return _dcg(rels) / d if d > 0 else 0.0


def _hit_at_k(ranked_ids: list[str], expected: set[str], k: int = 5) -> float:
    return 1.0 if any(rid in expected for rid in ranked_ids[:k]) else 0.0


def _mrr(ranked_ids: list[str], expected: set[str]) -> float:
    for i, rid in enumerate(ranked_ids):
        if rid in expected:
            return 1.0 / (i + 1)
    return 0.0


_TOOLS: dict | None = None


def _unwrap(r):
    return r if isinstance(r, dict) else r.structured_content


def _tools() -> dict:
    global _TOOLS
    if _TOOLS is None:
        import asyncio
        from fd_legal_search_mcp.server import create_server

        async def _names(s):
            return {t.name: t.fn for t in await s._list_tools()}

        _TOOLS = asyncio.run(_names(create_server()))
    return _TOOLS


def _run_case(domain: str, query: str, limit: int) -> list[str]:
    """直调对应工具面闭包，返回按序 stable_id（provenance.stable_id）。"""
    tools = _tools()
    if domain == "law":
        d = _unwrap(tools["law_search"](query, limit=limit))
        return [str(x["provenance"]["stable_id"]) for x in d["results"]]
    if domain == "case":
        d = _unwrap(tools["wenshu_search"](query, limit=limit))
        return [str(x["provenance"]["stable_id"]) for x in d["results"]]
    if domain == "guide":
        d = _unwrap(tools["guide_cases_search"](query, limit=limit))
        return [str(x["provenance"]["stable_id"]) for x in d["results"]]
    raise SystemExit(f"unknown domain {domain}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="eval/baseline.json")
    ap.add_argument("--out", default="eval/latest.json")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    entries = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    per_domain: dict[str, dict[str, list[float]]] = {}
    for e in entries:
        ranked = _run_case(e["domain"], e["query"], args.limit)
        expected = {str(x) for x in e["expected_stable_ids"]}
        m = per_domain.setdefault(e["domain"], {"ndcg10": [], "hit5": [], "mrr": []})
        m["ndcg10"].append(_ndcg_at_k(ranked, expected))
        m["hit5"].append(_hit_at_k(ranked, expected))
        m["mrr"].append(_mrr(ranked, expected))

    def agg(m: dict[str, list[float]]) -> dict:
        return {k: round(sum(v) / len(v), 4) if v else 0.0 for k, v in m.items()}

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_sha": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=str(ROOT)
        ).stdout.strip(),
        "domains": {d: agg(m) for d, m in sorted(per_domain.items())},
    }
    core = {d: v["ndcg10"] for d, v in result["domains"].items()}
    result["core"] = {"metric": "ndcg@10", "per_domain": core,
                      "macro": round(sum(core.values()) / len(core), 4) if core else 0.0}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))

    bp = Path(args.baseline)
    if bp.exists():
        base = json.loads(bp.read_text())
        regressions = []
        for d, v in result["domains"].items():
            b = base["domains"].get(d, {}).get("ndcg10", 0.0)
            if b > 0 and v["ndcg10"] < b * (1 - REL_DROP_GATE):
                regressions.append(f"{d}: ndcg@10 {b} -> {v['ndcg10']} (>{REL_DROP_KEY()})")
        if regressions:
            print("REGRESSION GATE FAILED:", "; ".join(regressions), file=sys.stderr)
            return 2
        print("regression gate: pass")
    else:
        print("no baseline — first run sets it (cp latest.json baseline.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
