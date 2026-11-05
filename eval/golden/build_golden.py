#!/usr/bin/env python3
"""法律检索金集生成器：对真实查询实查当前库，产出人工筛选的候选命中。

纪律（spec「金集条目锚定真数据」）：每个 expected_stable_id 都来自对当前库的
实查返回——不锚定不存在的行。生成流程 = 候选实查 → 人工筛选（去掉低相关的
 contains 噪音）→ 写 jsonl。本脚本只产候选（--stage candidates），人工筛选
后由 --stage finalize 写金集（把筛选结果固化为 canonical 文件）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fd_legal_search_mcp.config import Settings
from fd_legal_search_mcp.db import fetch_all, get_pool

CANDIDATES: list[dict] = [
    # --- 法条域（law_search 标题检索）：exact / prefix / partial ---
    {"domain": "law", "difficulty": "exact", "query": "中华人民共和国民法典"},
    {"domain": "law", "difficulty": "exact", "query": "中华人民共和国刑法"},
    {"domain": "law", "difficulty": "prefix", "query": "最高人民法院关于适用《中华人民共和国民法典》婚姻家庭编"},
    {"domain": "law", "difficulty": "partial", "query": "劳动争议调解仲裁"},
    {"domain": "law", "difficulty": "partial", "query": "电子商务促进与管理办法"},
    {"domain": "law", "difficulty": "exact", "query": "中华人民共和国民事诉讼法"},
    {"domain": "law", "difficulty": "partial", "query": "个人信息保护"},
    {"domain": "law", "difficulty": "partial", "query": "公司登记管理"},
    {"domain": "law", "difficulty": "prefix", "query": "中华人民共和国劳动合同法实施"},
    {"domain": "law", "difficulty": "prefix", "query": "网络数据安全管理条例"},
    # --- 类案域（wenshu_search 标题检索）：案号 exact / 案由 partial ---
    {"domain": "case", "difficulty": "partial", "query": "民间借贷纠纷民事判决书"},
    {"domain": "case", "difficulty": "partial", "query": "买卖合同纠纷"},
    {"domain": "case", "difficulty": "partial", "query": "民间借贷纠纷判决书"},
    {"domain": "case", "difficulty": "partial", "query": "劳动争议民事判决书"},
    {"domain": "case", "difficulty": "partial", "query": "房屋租赁合同纠纷"},
    {"domain": "case", "difficulty": "partial", "query": "机动车交通事故责任纠纷"},
    {"domain": "case", "difficulty": "partial", "query": "金融借款合同纠纷"},
    {"domain": "case", "difficulty": "partial", "query": "侵害商标权纠纷"},
    {"domain": "case", "difficulty": "partial", "query": "股权转让纠纷"},
    {"domain": "case", "difficulty": "partial", "query": "不当得利纠纷"},
    # --- 指导案例域（guide_cases_search）---
    {"domain": "guide", "difficulty": "exact", "query": "指导案例9号"},
    {"domain": "guide", "difficulty": "partial", "query": "指导案例9号：上海存亮贸易有限公司"},
    {"domain": "guide", "difficulty": "partial", "query": "指导案例123号：于红岩"},
    {"domain": "guide", "difficulty": "partial", "query": "指导案例20号：深圳市斯瑞曼"},
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["candidates", "finalize"], default="candidates")
    ap.add_argument("--out", default="eval/golden/candidates.jsonl")
    args = ap.parse_args()

    settings = Settings.from_env()
    if args.stage == "candidates":
        pool_law = get_pool("FDLEX_LAW_DATABASE_URL", settings.law_database_url)
        pool_case = get_pool("FDLEX_WENSHU_DATABASE_URL", settings.wenshu_database_url)
        out = []
        for c in CANDIDATES:
            q = c["query"]
            if c["domain"] == "case":
                rows = fetch_all(
                    pool_case,
                    'SELECT j."doc_id" AS sid, j."标题" AS title FROM judgments j WHERE j."标题" ILIKE %s '
                    'ORDER BY CASE WHEN lower(j."标题")=lower(%s) THEN 0 '
                    "WHEN lower(j.\"标题\") LIKE lower(%s) || '%%' THEN 1 ELSE 2 END LIMIT 5",
                    ("%" + q + "%", q, q),
                )
                hits = [{"stable_id": r["sid"], "title": (r["title"] or "")[:60]} for r in rows]
            else:
                table = "laws" if c["domain"] == "law" else "guide_cases"
                idc = "id" if c["domain"] == "law" else "source_id"
                rows = fetch_all(
                    pool_law,
                    f"SELECT {idc} AS sid, title FROM {table} WHERE title ILIKE %s "
                    "ORDER BY CASE WHEN lower(title)=lower(%s) THEN 0 "
                    "WHEN lower(title) LIKE lower(%s) || '%%' THEN 1 ELSE 2 END LIMIT 5",
                    ("%" + q + "%", q, q),
                )
                hits = [{"stable_id": r["sid"], "title": (r["title"] or "")[:60]} for r in rows]
            out.append({**c, "candidates": hits})
            print(f"{c['domain']}/{c['difficulty']}: {q} -> {len(hits)} hits", file=sys.stderr)
        Path(args.out).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in out), encoding="utf-8"
        )
        print(f"candidates -> {args.out} ({len(out)} queries)")
        return
    raise SystemExit("finalize 阶段：人工筛选 candidates.jsonl 后以 canonical 金集提交（1.3 一步完成）")


if __name__ == "__main__":
    main()
