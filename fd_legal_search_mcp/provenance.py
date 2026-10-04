"""溯源契约（spec lex-mcp-surface）：每个结果携带 source / stable_id / as_of。

缺三件套即 bug，不是可降级项——无法溯源的结果不返回。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provenance:
    source: str  # 如 "law_db.law_chunks_v2"
    stable_id: str  # 来源库内稳定标识（如 chunk id / (2020)京01民初1号 / 指导案例编号）
    as_of: str  # ISO-8601 数据口径日期

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "stable_id": self.stable_id, "as_of": self.as_of}


def with_provenance(item: dict, provenance: Provenance) -> dict:
    """把三件套挂到结果条目上（实现任务填数据时统一走这里）。"""
    return {**item, "provenance": provenance.to_dict()}