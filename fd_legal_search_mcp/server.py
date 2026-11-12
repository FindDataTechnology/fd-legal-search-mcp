"""识律(lex)法律检索 FastMCP 服务器。

工具面 = business-mcp 法律域（law/legal_docs/guide_cases/wenshu）的迁移目标
+ 两个新能力（law_semantic_search 走 Qdrant、law_reference 条级引用解析）。
骨架阶段：每个工具以指名 tracking task 的诚实错误回报——绝不编造数据。
"""

from __future__ import annotations

import functools

from fastmcp import FastMCP

from . import tools_case, tools_corpus, tools_guide, tools_law
from .config import Settings
from . import workflows

SERVER_NAME = "fd-legal-search-mcp"

INSTRUCTIONS = (
    "识律法律检索：法条检索与条级引用解析、类案检索（裁判文书）、指导案例检索、"
    "五源法律语料检索。所有结果携带 provenance（source/stable_id/as_of）；"
    "未命中如实返回空结果，绝不编造。骨架阶段工具以诚实错误回报未接线状态。"
)


def create_server(settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    mcp: FastMCP = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)

    from . import wellknown

    wellknown.register(mcp)  # /.well-known/line-manifest.json（公开、免鉴权、零网络）
    tools_law.register(mcp, settings)
    tools_case.register(mcp, settings)
    tools_guide.register(mcp, settings)
    tools_corpus.register(mcp, settings)

    # 工作流级知识随服务暴露（mcp-surface-contract 组 7）：调用方需要时才拉取，
    # 不必塞进每个工具的 description 里被反复读进上下文。
    for uri, (title, text) in workflows.RESOURCES.items():
        # 每个 URI 一个闭包：直接捕获循环变量会让三份资源都指向最后一份。
        mcp.resource(uri, name=title, mime_type="text/markdown")(
            functools.partial(lambda _t: _t, text))
    return mcp