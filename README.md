# fd-legal-search-mcp

识律（lex 线）法律检索 MCP：法条检索与条级引用解析、类案检索（裁判文书）、指导案例检索、五源法律语料——逐结果溯源，可私有化部署。

新仓依据 `openspec/changes/lex-platform-v1`（task 1.1）。本服务是 2026-09-19「不新建 server」决定的翻案落地：`wenshu_db` 在库已消解「类案检索无数据源」的前提。

> **数据量口径**：裁判文书按 `fd-lex/collateral/numbers.md` 的 N3/N4 计——**3,700 万（37,001,012）**，行数与篇数同值（`doc_id` 全表唯一，装载链已去重）。本文件早前写的 1,050 万是历史快照，已废止；对外口径一律引 numbers.md。

## 工具面（契约）

| 工具 | 能力 | 数据源 | 实现跟踪 |
|---|---|---|---|
| `law_search` / `law_read` | 法条检索 / 法规详情 | `law_db.laws` | task 7.1（迁自 business-mcp，等价对账） |
| `law_semantic_search` | 法条语义检索 | Qdrant `law_chunks_v2`（247,364 点，1024 维） | task 1.2 |
| `law_reference` | 条级引用解析 | 法规目录（32,275 部） | task 1.2 |
| `wenshu_search` / `wenshu_read` | 类案检索 / 裁判文书正文 | `wenshu_db`（3,700 万篇，见上方口径注）+ RustFS zstd 块 | task 1.3 |
| `guide_cases_search` / `guide_cases_read` | 指导案例检索 / 读取 | `law_db.guide_cases`（278+15） | task 1.4 |
| `legal_docs_search` / `legal_docs_read` | 五源语料检索 / 读取 | `law_db.legal_documents`（29,958 篇） | task 7.1（迁自 business-mcp，等价对账） |

骨架阶段：全部工具以诚实错误回报未接线状态（消息含 tracking task），不返回编造数据；未命中一律如实返回空结果。

## 溯源契约

每个结果携带 `provenance`：`source` / `stable_id` / `as_of`。缺失即 bug，不做降级。
（见 `openspec/changes/lex-platform-v1/specs/lex-mcp-surface/spec.md`）

## 配置

全部走环境变量（见 `.env.example`）。零配置即可启动——握手与工具枚举可用；工具调用时对缺失配置如实回报 `upstream_unavailable` 并指名缺失变量。

## 本地开发

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
.venv/bin/fd-legal-search-mcp serve            # http://127.0.0.1:8000
.venv/bin/fd-legal-search-mcp serve --transport stdio
```

## 发布通道

GitHub Actions -> TCR（`hkccr.ccs.tencentyun.com/finddata-business/fd-legal-search-mcp`，
tag `sha-<短哈希>` + `main`；cheap-3 tcr-relay 5 分钟内回灌 `ccr` 侧）。配方：`docs/IMAGE-RELEASE.md`。
Jenkins->Harbor 为回退通道。