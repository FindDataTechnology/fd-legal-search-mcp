# fd-legal-search-mcp image — 识律(lex) 法律检索 MCP。
#
# 发布通道：GitHub Actions -> TCR（.github/workflows/image.yml，配方 docs/IMAGE-RELEASE.md）。
# 硬性要求（IMAGE-RELEASE 六步清单）：上游源为默认（美国 runner 绕道国内镜像会把
# 构建拖成小时级），国内镜像源一律 ARG 且默认空；单层 <300MB。
FROM python:3.12-slim

ARG PIP_INDEX_URL=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY fd_legal_search_mcp ./fd_legal_search_mcp

RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" .; else pip install --no-cache-dir .; fi \
 && useradd -m -u 1000 appuser

USER appuser

EXPOSE 8000

CMD ["fd-legal-search-mcp", "serve", "--host", "0.0.0.0", "--port", "8000"]