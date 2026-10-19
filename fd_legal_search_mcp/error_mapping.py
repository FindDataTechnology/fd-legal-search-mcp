"""领域异常 → 封闭错误码（mcp-error-semantics 组 4）。

本服务的领域异常已经是三分类（`NotFound` / `Validation` /
`UpstreamUnavailable`，见 `errors.py`），但它们是**普通异常**：fastmcp 捕获
后包成一段文本、`structuredContent` 为空，调用方只能读字符串判断类别。

包一层后，三类的码与 `isError` 同时到达客户端：

| 领域异常 | 码 | 重试 |
|---|---|---|
| `Validation` | `invalid_input` | 否 |
| `NotFound` | `not_found` | 否 |
| `UpstreamUnavailable` | `upstream_unavailable` | 是 |
| `not_wired`（ToolError） | `not_wired` | 否 |

`not_wired` 目前没有调用点（五源已接线，由 `test_stub_semantics.py` 静态
钉住），但映射保留：它对应"能力尚未提供"这一类，删掉映射会让未来重新引入
未接线工具时又退化成一段文本。

**未知异常照常抛**：真正的 bug 要带堆栈进日志，不能伪装成可重试错误。
"""
from __future__ import annotations

import functools
from typing import Any, Callable

from .errors import LexError, NotFound, UpstreamUnavailable, Validation
from .error_codes import INVALID_INPUT, NOT_FOUND, NOT_WIRED, UPSTREAM_UNAVAILABLE, error_result


def _classify(exc: BaseException) -> tuple[str, str, dict[str, Any]] | None:
    if isinstance(exc, Validation):
        return INVALID_INPUT, str(exc), {}
    if isinstance(exc, NotFound):
        return NOT_FOUND, str(exc), {}
    if isinstance(exc, UpstreamUnavailable):
        return UPSTREAM_UNAVAILABLE, str(exc), {"upstream": _upstream_name(exc)}
    if isinstance(exc, LexError):
        # 基类兜底：新加的领域异常不该默默变成"未知异常"抛出去。
        return INVALID_INPUT, str(exc), {}
    return None


def _upstream_name(exc: BaseException) -> str:
    """从消息里认出是哪个上游（wenshu_db / law_db / qdrant / embedder）。

    消息是唯一线索——领域层没有把上游名做成字段。这不是理想做法，但比让
    调用方自己正则匹配要稳定：命名口径集中在本函数一处。
    """
    text = str(exc).lower()
    # 环境变量名也要认：未配置时的消息是「缺少环境变量 FDLEX_LAW_DATABASE_URL」，
    # 里面并不含 "law_db" 字样。只认库名会让最常见的"没配"场景全部落到
    # unknown_upstream，而那正是最需要指出是哪个上游的时刻。
    for name, needles in (
        ("wenshu_db", ("wenshu", "fdlex_wenshu")),
        ("law_db", ("law_db", "fdlex_law")),
        ("qdrant", ("qdrant",)),
        ("embedder", ("embed",)),
        ("rustfs", ("rustfs",)),
    ):
        if any(n in text for n in needles):
            return name
    return "unknown_upstream"


def map_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """包一层：领域异常 → 结构化错误码；未知异常照常抛。"""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 — 分类后未知异常会重新抛出
            classified = _classify(exc)
            if classified is None:
                raise
            code, message, extra = classified
            return error_result(code, message, **extra)

    return wrapper
