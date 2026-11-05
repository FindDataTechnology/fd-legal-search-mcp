"""检索质量回归门的离线段：金集锚定校验（不连库，验 id 形状/域/难度合法）。"""
import json
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "eval" / "golden" / "legal-retrieval.jsonl"


def test_golden_set_shape_and_anchors():
    entries = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) >= 20, "金集规模过小，回归门失去代表性"
    domains = {e["domain"] for e in entries}
    assert domains == {"law", "case", "guide"}, f"三域不齐: {domains}"
    for e in entries:
        assert e["query"].strip(), "空查询"
        assert e["difficulty"] in ("exact", "prefix", "partial")
        assert e["expected_stable_ids"], f"无期望命中: {e['query']}"


def test_regression_gate_threshold():
    """门阈值语义：>5% 相对降幅才失败；基线为 0 时不判回退（首轮保护）。"""
    from eval.run_retrieval_eval import REL_DROP_GATE

    def failed(base: float, now: float) -> bool:
        return base > 0 and now < base * (1 - REL_DROP_GATE)

    assert not failed(0.0, 0.0)      # 基线 0 → 不判
    assert not failed(0.5, 0.48)     # 4% 降幅 → 过
    assert failed(0.5, 0.47)         # 6% 降幅 → 拦
