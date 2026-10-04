"""环境驱动配置。

零配置哲学：无配置也能启动（握手与工具枚举可用）；工具调用对缺失配置如实
回报 UpstreamUnavailable 并指名缺失变量。无配置文件，无遮住"未配置"的默认值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    law_database_url: str | None
    wenshu_database_url: str | None
    qdrant_url: str | None
    qdrant_collection: str
    qdrant_api_key: str | None
    rustfs_endpoint: str | None
    rustfs_bucket: str
    rustfs_access_key: str | None
    rustfs_secret_key: str | None
    embed_backend: str
    embed_api_url: str | None
    embed_api_key: str | None
    embed_model: str
    embed_dim: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            law_database_url=_env("FDLEX_LAW_DATABASE_URL"),
            wenshu_database_url=_env("FDLEX_WENSHU_DATABASE_URL"),
            qdrant_url=_env("FDLEX_QDRANT_URL"),
            qdrant_collection=_env("FDLEX_QDRANT_COLLECTION", "law_chunks_v2") or "law_chunks_v2",
            qdrant_api_key=_env("FDLEX_QDRANT_API_KEY"),
            rustfs_endpoint=_env("FDLEX_RUSTFS_ENDPOINT"),
            rustfs_bucket=_env("FDLEX_RUSTFS_BUCKET", "wenshu") or "wenshu",
            rustfs_access_key=_env("FDLEX_RUSTFS_ACCESS_KEY"),
            rustfs_secret_key=_env("FDLEX_RUSTFS_SECRET_KEY"),
            embed_backend=_env("FDLEX_EMBED_BACKEND", "api") or "api",
            embed_api_url=_env("FDLEX_EMBED_API_URL"),
            embed_api_key=_env("FDLEX_EMBED_API_KEY"),
            embed_model=_env("FDLEX_EMBED_MODEL", "Qwen/Qwen3-Embedding-0.6B") or "",
            embed_dim=int(_env("FDLEX_EMBED_DIM", "1024") or "1024"),
        )