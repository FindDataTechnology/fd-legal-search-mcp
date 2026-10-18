"""骨架诚实性：本服务**不允许**再出现未接线的占位工具。

历史：`legal_docs_search` / `legal_docs_read` 曾以 `not_wired(...)` 失败并指名
tracking task（lex-platform-v1 task 7.1）。两者已于 finance-legal-trust-gates
§5 接线，`not_wired` 不再有调用点。

这个文件保留下来守一条规则：**新加工具必须同时落地实现**。若将来有人再挂
一个 `raise not_wired(...)`，这里的静态扫描会先红，而不是等到客户调用时才
发现「工具面声明了但不可用」——那正是本变更要根治的缺陷类型。
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "fd_legal_search_mcp"


def test_no_tool_raises_not_wired():
    offenders = []
    for path in sorted(PKG.glob("*.py")):
        if path.name == "errors.py":  # not_wired 的定义处
            continue
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"raise\s+not_wired\(([^)]*)\)", text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line} {m.group(0)}")
    assert offenders == [], (
        "工具面仍有未接线的占位实现（声明的工具必须可调用）：\n  "
        + "\n  ".join(offenders)
    )
