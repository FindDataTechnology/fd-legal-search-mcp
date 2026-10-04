"""工具错误语义（建模自 business-mcp 的四类哲学：NotFound / Validation /
UpstreamUnavailable 以工具错误返回；未知故障抛出）。本模块不编造任何数据。"""

from __future__ import annotations

from fastmcp.exceptions import ToolError


class LexError(Exception):
    """领域错误基类，映射为工具错误。"""

    kind = "lex_error"


class Validation(LexError):
    kind = "validation"


class NotFound(LexError):
    kind = "not_found"


class UpstreamUnavailable(LexError):
    kind = "upstream_unavailable"


def not_wired(tool: str, task: str) -> ToolError:
    """骨架阶段的诚实占位错误：契约存在，接线未到。

    消息指名 tracking task，给调用方修复路径；绝不返回填充数据。
    """
    return ToolError(
        f"{tool} 尚未接线（骨架阶段）。实现跟踪：lex-platform-v1 task {task}；本服务不返回编造数据。"
    )