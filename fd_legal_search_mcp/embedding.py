"""查询嵌入后端。

law_chunks_v2 用 Qwen/Qwen3-Embedding-0.6B（1024 维）建库，查询必须同模型，
否则相似度静默劣化。v1 = API 后端（复用 flk-embed-cronjob 的调用约定）；
本地权重随交付包落地见 lex-platform-v1 task 6.6。
"""

from __future__ import annotations

import httpx

from .config import Settings
from .errors import UpstreamUnavailable


class ApiEmbedder:
    def __init__(self, url: str, api_key: str, model: str, dim: int) -> None:
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = httpx.post(
            f"{self._url}/embeddings",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": self._model, "input": texts},
            timeout=30,
        )
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        return [d["embedding"] for d in data]


def get_embedder(settings: Settings) -> ApiEmbedder:
    if settings.embed_backend != "api":
        raise UpstreamUnavailable(
            f"FDLEX_EMBED_BACKEND={settings.embed_backend!r} 暂不支持（v1 仅 api；本地权重见 task 6.6）"
        )
    if not (settings.embed_api_url and settings.embed_api_key):
        raise UpstreamUnavailable("未配置 Embedding API：缺少 FDLEX_EMBED_API_URL / FDLEX_EMBED_API_KEY")
    return ApiEmbedder(
        settings.embed_api_url,
        settings.embed_api_key,
        settings.embed_model,
        settings.embed_dim,
    )