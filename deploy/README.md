> **部署清单已迁走（2026-11-20）**
> 本仓不再承载集群清单。清单在 `fd-infra-deploy/all-services/fd-legal-search-mcp/`，
> Application 定义在 `fd-infra-deploy/argocd/app-fd-legal-search-mcp.yaml`。
> 原因与判据：`openspec/changes/consolidate-deploy-manifests`。

# fd-legal-search-mcp 部署 runbook（lex-platform-v1）

通道：tcr-image-pipeline（GHA→TCR→relay）+ mcp-service-deploy（ArgoCD/GitOps、不可变 sha- tag）。

| 工件 | 作用 |
|---|---|
| `deploy/k8s/fd-legal-search-mcp.yaml` | Deployment + Service（NodePort 31884，fd-prod） |
| `deploy/argocd/application.yaml` | ArgoCD Application（gitee 源 `deploy/k8s`，dest `fd-prod`） |

## Secret `fd-legal-search-env` 键名表（集群内创建，永不入 git）

与 Deployment `envFrom` 的 `.env.example` 逐键一致（13 键；生产取值见 INFRA-NOTES / fd-mcp-env 同源）：

| env | 用途 |
|---|---|
| `FDLEX_LAW_DATABASE_URL` | law_db（fdbiz_ro@100.64.0.5:5432/law_db，只读） |
| `FDLEX_WENSHU_DATABASE_URL` | wenshu_db（同实例另一库） |
| `FDLEX_QDRANT_URL` | Qdrant（http://100.64.0.9:6333） |
| `FDLEX_QDRANT_COLLECTION` | law_chunks_v2 |
| `FDLEX_QDRANT_API_KEY` | 留空（集群内网无鉴权） |
| `FDLEX_RUSTFS_ENDPOINT` | http://100.64.0.11:9000 |
| `FDLEX_RUSTFS_BUCKET` | wenshu |
| `FDLEX_RUSTFS_ACCESS_KEY` / `FDLEX_RUSTFS_SECRET_KEY` | RustFS 只读凭据 |
| `FDLEX_EMBED_BACKEND` | api |
| `FDLEX_EMBED_API_URL` | https://api.siliconflow.cn/v1 |
| `FDLEX_EMBED_API_KEY` | 硅基流动 key（同 xinru scraw/flk-embed-credentials） |
| `FDLEX_EMBED_MODEL` | Qwen/Qwen3-Embedding-0.6B |
| `FDLEX_EMBED_DIM` | 1024 |

## 上线步骤

1. push main → GHA 出 `sha-<7>` tag；relay 回灌 ccr（cheap-3 `/var/log/tcr-relay.log` 确认）。
2. 回填 `deploy/k8s/fd-legal-search-mcp.yaml` 的镜像 tag，提交。
3. 集群建 Secret（键名表逐键一致）+ argocd ns 建 repo 凭据 secret（`repo-fd-legal-search-mcp`，url/username/password/type 四键 + label `argocd.argoproj.io/secret.type=repository`）。
4. `kubectl --context cheap -n argocd apply -f deploy/argocd/application.yaml` → 观察 fd-prod 滚起。
5. 验收：`curl http://<node>:31884/mcp` 非 404（MCP 端点在；未带鉴权头的行为按 registry 侧入口为准——本服务自身无鉴权，对外一律经 registry lex 入口）。
