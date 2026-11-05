# 检索质量门（spec: legal-retrieval-quality）

- `golden/legal-retrieval.jsonl` — 金集（24 条：law/case/guide 三域，exact/prefix/partial 难度）。
  期望命中全部锚定当前库实查（`golden/build_golden.py --stage candidates` 产候选，人工筛选定稿）。
- `run_retrieval_eval.py` — 直调工具闭包跑分（nDCG@10/hit@5/MRR 分域分解）。
  `--baseline eval/baseline.json` 对照，5% 相对降幅门（exit 2）。
- `baseline.json` — 首轮基线（诚实基线：case 域 0.1 = 排序契约缺陷被金集首证时的状态）。

## 已证缺陷（金集首功）
wenshu_search 候选阶段「无序 LIMIT 2000」在 contains 命中 >> 2000 时把 exact/prefix 段
完全吞掉（『买卖合同纠纷』exact 48 行全不进窗）。已修：三段分窗候选（exact 50/prefix 500/
contains 2000 合并重排）。修复后 case 域 nDCG@10 0.1→0.52。

## 运行
```sh
set -a && source .env && set +a
python3 eval/run_retrieval_eval.py --baseline eval/baseline.json --out eval/latest.json
# 门失败（exit 2）= 核心指标回退；修代码或论证后更新 baseline
```
