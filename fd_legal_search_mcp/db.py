"""惰性数据源访问器：PostgreSQL 连接池、Qdrant 客户端、RustFS(S3) 客户端。

一切在首次使用时才连接——服务器零配置即可启动；缺 URL 抛
UpstreamUnavailable 并指名环境变量，诚实优于导入即崩。
"""

from __future__ import annotations

import threading
from typing import Any

from .config import Settings
from .errors import UpstreamUnavailable

_lock = threading.Lock()
_pools: dict[str, Any] = {}
_qdrant: Any = None
_rustfs: Any = None


def get_pool(env_name: str, url: str | None) -> Any:
    """返回 key 对应的惰性 psycopg 连接池。"""
    if not url:
        raise UpstreamUnavailable(f"未配置数据源：缺少环境变量 {env_name}")
    with _lock:
        pool = _pools.get(env_name)
        if pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(url, min_size=0, max_size=4, timeout=10, name=f"fdlex-{env_name}")
            _pools[env_name] = pool
        return pool


def get_qdrant(settings: Settings) -> Any:
    global _qdrant
    if not settings.qdrant_url:
        raise UpstreamUnavailable("未配置向量库：缺少环境变量 FDLEX_QDRANT_URL")
    with _lock:
        if _qdrant is None:
            from qdrant_client import QdrantClient

            _qdrant = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key or None,
                timeout=20,
            )
        return _qdrant


def get_rustfs(settings: Settings) -> Any:
    global _rustfs
    if not (settings.rustfs_endpoint and settings.rustfs_access_key and settings.rustfs_secret_key):
        raise UpstreamUnavailable(
            "未配置对象存储：缺少 FDLEX_RUSTFS_ENDPOINT / FDLEX_RUSTFS_ACCESS_KEY / FDLEX_RUSTFS_SECRET_KEY"
        )
    with _lock:
        if _rustfs is None:
            import boto3

            _rustfs = boto3.client(
                "s3",
                endpoint_url=settings.rustfs_endpoint,
                aws_access_key_id=settings.rustfs_access_key,
                aws_secret_access_key=settings.rustfs_secret_key,
                region_name="us-east-1",
            )
        return _rustfs


def fetch_all(pool: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """执行只读查询并返回 dict 行列表。"""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_one(pool: Any, sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = fetch_all(pool, sql, params)
    return rows[0] if rows else None