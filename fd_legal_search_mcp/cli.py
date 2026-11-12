"""fd-legal-search-mcp CLI。"""

from __future__ import annotations

import click

from . import __version__


@click.group()
@click.version_option(__version__, prog_name="fd-legal-search-mcp")
def cli() -> None:
    """识律(lex) 法律检索 MCP 服务。"""


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, type=int, show_default=True)
@click.option(
    "--transport",
    type=click.Choice(["http", "sse", "stdio"]),
    default="http",
    show_default=True,
)
def serve(host: str, port: int, transport: str) -> None:
    """运行 MCP 服务器（容器用 http，本地助手接线用 stdio）。"""
    from .auth import BearerAuthMiddleware, configured_token
    from .server import create_server

    server = create_server()
    if transport == "stdio":
        server.run(transport="stdio")
        return

    if not configured_token():
        # 未配 token = 与接入前逐字节一致（不挂任何中间件）。
        server.run(transport=transport, host=host, port=port)
        return

    # 配了 token 才装配带门的 ASGI 面（change converge-line-identity-and-keys 8.6）。
    import uvicorn
    from starlette.middleware import Middleware

    uvicorn.run(
        server.http_app(middleware=[Middleware(BearerAuthMiddleware)]),
        host=host,
        port=port,
    )


if __name__ == "__main__":
    cli()