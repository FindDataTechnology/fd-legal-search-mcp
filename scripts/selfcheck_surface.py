#!/usr/bin/env python3
"""tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

断言两个方向：
  1. 一致：manifest 声明的工具 == 代码注册的工具（新增/删除工具而 manifest
     未同步即失败，反向同理）
  2. 闭合：任何消费侧暴露的工具集 ⊆ manifest 的 commercial 集
     ——消费侧由 --consumers 传入的 pack/套件目录提供

代码注册面取 `mcp.list_tools()`（运行时真值，优先于 grep 码点）。

用法：
    python3 scripts/selfcheck_surface.py                    # 只做方向 1
    python3 scripts/selfcheck_surface.py \
        --consumers /path/to/docs/legal-presets/suites      # 加方向 2
退出码：0 通过 / 1 失败（列出差异）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tool-surface.json"
SERVER_NAME = "fd-legal-search-mcp"

TOOL_PATTERN = re.compile(rf"mcp__{SERVER_NAME}__([A-Za-z0-9_.\-]+)")


def load_manifest() -> dict:
    if not MANIFEST.exists():
        fail([f"manifest 缺失：{MANIFEST}（契约：缺失即未上牌）"])
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def manifest_tools(m: dict) -> dict[str, str]:
    """工具名 → 暴露级别。"""
    out = {}
    for g in m.get("tool_groups", []):
        level = g.get("level")
        if level not in ("commercial", "internal"):
            fail([f"tool_groups.{g.get('group')} 的 level 非法：{level!r}"])
        for t in g.get("tools", []):
            if t in out and out[t] != level:
                fail([f"工具 {t} 在多个 group 中级别冲突"])
            out[t] = level
    return out


async def runtime_tools() -> set[str]:
    """从运行时取真值；server 不可导入时回退到源码码点。"""
    names = await runtime_annotation_map()
    if names is None:
        return source_tools()
    return set(names)


async def runtime_annotation_map() -> dict[str, dict] | None:
    """运行时每个工具的行为属性（协议层原样值）；server 不可导入时返回 None。"""
    sys.path.insert(0, str(REPO))
    try:
        from fd_legal_search_mcp.server import create_server

        tools = await create_server().list_tools()
    except Exception:
        return None
    out: dict[str, dict] = {}
    for t in tools:
        name = getattr(t, "name", None) or t.get("name")
        if not name:
            continue
        a = getattr(t, "annotations", None)
        if a is None:
            out[name] = {}
            continue
        out[name] = {
            "readOnlyHint": a.readOnlyHint,
            "destructiveHint": a.destructiveHint,
            "idempotentHint": a.idempotentHint,
            "openWorldHint": a.openWorldHint,
        }
    return out


REQUIRED_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


async def check_annotations(m: dict, declared: dict[str, str]) -> list[str]:
    """manifest 行为属性 ↔ 运行时注册值逐项相等（两个方向都查）。

    漏登记 → 客户端拿到 None，无从判断自动批准；谎报只读 → 客户端自动批准一个
    会写库的工具。两者都是上牌缺陷。
    """
    ann = m.get("tool_annotations")
    if not isinstance(ann, dict):
        return ["manifest 缺 tool_annotations（行为属性须随工具一并上牌）"]
    runtime = await runtime_annotation_map()
    if runtime is None:
        return ["[无法核对] server 不可导入，行为属性无法与运行时比对"]

    problems: list[str] = []
    for name in sorted(set(declared) - set(ann)):
        problems.append(f"{name}: manifest 未声明行为属性")
    for name in sorted(set(ann) - set(declared)):
        problems.append(f"{name}: tool_annotations 登记了未上牌的工具")
    for name in sorted(set(declared) & set(ann)):
        d = ann[name]
        missing = [k for k in REQUIRED_HINTS if k not in d]
        if missing:
            problems.append(f"{name}: 属性不全，缺 {missing}")
            continue
        rt = runtime.get(name, {})
        for k in REQUIRED_HINTS:
            if rt.get(k) != d[k]:
                problems.append(
                    f"{name}.{k}: manifest 声明 {d[k]!r} 与运行时 {rt.get(k)!r} 不符"
                )
    return problems


def source_tools() -> set[str]:
    """回退：扫 @mcp.tool 装饰的函数名。"""
    names: set[str] = set()
    for f in (REPO / "fd_legal_search_mcp").glob("tools_*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"@mcp\.tool\s*(?:\([^)]*\))?\s*\n\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)", text):
            names.add(m.group(1))
    return names


def consumer_tools(consumers: list[Path]) -> dict[str, set[str]]:
    """消费侧（套件/技能）**必需依赖**中实际点名的本 server 工具。

    判定：frontmatter 的 `data_binding` 行 = 必需依赖（受闭合约束）；
    `optional_binding` 行 = 可选增强（不受约束，但消费侧必须自行处理不可用）。
    正文里为说明降级而提及 internal 工具不算越界——看的是依赖声明。
    """
    out: dict[str, set[str]] = {}
    for c in consumers:
        if not c.exists():
            fail([f"消费侧路径不存在：{c}"])
        used: set[str] = set()
        for f in list(c.rglob("*.json")) + list(c.rglob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line in text.splitlines():
                if re.match(r"\s*(data_binding|mcp_servers)\s*:", line):
                    used |= set(TOOL_PATTERN.findall(line))
        if used:
            out[str(c)] = used
    return out


def fail(problems: list[str]) -> "None":
    print(f"✗ {len(problems)} 项不通过：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)
def check_output_structure(m: dict, declared: dict[str, str]) -> list[str]:
    """输出结构状态必须显式登记（mcp-result-contract「无静默空缺」）。

    本服务尚未逐工具声明输出结构（返回形状随输入变化，需先固化契约），故
    manifest 里登记为 `undeclared`。这条检查保证：新增工具时不会出现"既没声明、
    也没登记为未声明"的静默空缺——那种工具在客户端看来与已声明工具无从区分。

    日后某工具真的挂上 output_schema 时，本检查会失败并提示把它移进 declared：
    登记状态与运行时事实必须一致，不能靠记忆维护。
    """
    block = m.get("output_structure")
    if not isinstance(block, dict):
        return ["manifest 缺 output_structure（输出结构状态须显式可见）"]
    dec = set(block.get("declared") or [])
    und = set(block.get("undeclared") or [])
    problems: list[str] = []
    both = sorted(dec & und)
    if both:
        problems.append(f"同时出现在 declared 与 undeclared：{both}")
    silent = sorted(set(declared) - dec - und)
    if silent:
        problems.append(f"既未声明也未登记为未声明（静默空缺）：{silent}")
    stray = sorted((dec | und) - set(declared))
    if stray:
        problems.append(f"output_structure 登记了未上牌的工具：{stray}")
    return problems



async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumers", nargs="*", type=Path, default=[])
    args = ap.parse_args()

    m = load_manifest()
    declared = manifest_tools(m)
    actual = await runtime_tools()
    problems: list[str] = []

    # 方向 1：一致
    missing_in_manifest = sorted(actual - set(declared))
    stale_in_manifest = sorted(set(declared) - actual)
    if missing_in_manifest:
        problems.append(f"[注册表漂移] 代码已注册但 manifest 未声明：{', '.join(missing_in_manifest)}")
    if stale_in_manifest:
        problems.append(f"[manifest 陈旧] manifest 声明但代码未注册：{', '.join(stale_in_manifest)}")

    expected_count = m.get("registry", {}).get("expected_tool_count")
    if expected_count is not None and len(actual) != expected_count:
        problems.append(f"[工具数不符] 运行时 {len(actual)} ≠ manifest 声明 {expected_count}")

    problems += await check_annotations(m, declared)
    problems += check_output_structure(m, declared)

    # 方向 2：闭合（消费侧 ⊆ commercial）
    internal = {t for t, lv in declared.items() if lv == "internal"}
    if args.consumers:
        for where, used in consumer_tools(args.consumers).items():
            over = sorted(used & internal)
            if over:
                problems.append(
                    f"[消费侧越界] {where} 点名了 internal 工具：{', '.join(over)}"
                    f"（manifest 定级 internal，不得进商业供给面）"
                )

    commercial = sorted(t for t, lv in declared.items() if lv == "commercial")
    print(f"manifest：{len(declared)} 工具（commercial {len(commercial)} / internal {len(internal)}）")
    print(f"运行时：{len(actual)} 工具")
    if internal:
        print(f"internal（消费侧不得点名）：{', '.join(sorted(internal))}")

    if problems:
        fail(problems)
    print("✓ selfcheck 双向断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
