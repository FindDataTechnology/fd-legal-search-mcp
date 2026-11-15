# 识律（legal-search）线的授权与凭据证据

本文件是 `line-manifest.json` 被剥离的自由文本的**仓内**留存。原因：清单会随
`/.well-known/line-manifest.json` **匿名公开**，而规格（`openspec` 的
`line-manifest` 能力）规定清单字符串键只能落在一个闭集里——闭集之外的自由文本
（典型是 `note`）一律判违规：一段自由文本不是「写短点」就安全，它压根不该出现在
公开契约里。下列内容含 Secret 名、环境变量名、内部代码路径与豁免面清单，故只留在
仓库内，不进公开载荷。

对外可见的那部分是产品语义：本线提供 10 个只读工具（法条检索与条级引用解析 /
类案 / 指导案例 / 五源语料），逐结果溯源，可私有化部署（私有化包里由客户自己的
前置代理做鉴权）。

## 被剥离的 note（原文逐字，按清单路径索引）

### `<root>`

10 个只读工具：法条检索与条级引用解析 / 类案 / 指导案例 / 五源语料；逐结果溯源，可私有化部署（私有化包里由客户自己的前置代理做鉴权）。 **8.6 线自有钥匙**：`auth.py` 与 fd-info/fd-text-core/fd-open-data-mcp/fd-cn-report 的门同族；门对 /health(z) 精确豁免，探针不受影响。

### `identity`

线自有共享 bearer 门**已在生产生效**（2026-11-12 翻转，change converge-line-identity-and-keys 8.6）：`MCP_BEARER_TOKEN` 由线级 Secret 注入，注册表该 server 行 `auth_scheme=bearer` + `auth_credential` 为同一把令牌（出口把 `Authorization` 换成它）；匿名直连 401、带令牌 200。同时提供线自描述端点 `/.well-known/line-manifest.json`（公开、免鉴权、零网络，`Cache-Control: public, max-age=300`），**原样回吐**本仓 `line-manifest.json` 的字节；该路径在门的 `EXEMPT_PATHS` 里，所以未持凭据的平台/客户也能读到「这条线认哪些钥匙」。仓内数据库/对象存储凭据都是**出站**连接串，与本线入站门无关。这把钥匙是**线级**共享钥匙，一把全客户共用，不承担客户级身份（谁在用由网关/注册表记录）；每客户一把需要钥匙库 + 吊销面，属后续 8.x。`jit_policy=off`：无断言兑换端点。

### `auth_tracks[]`

线自有共享钥匙（`MCP_BEARER_TOKEN`），**已在运行路径生效**（2026-11-12 翻转，change converge-line-identity-and-keys 8.6）。翻转两步：① 注册表服务器级 backend auth（该 server 行 `auth_scheme=bearer` + `auth_credential` = 同一把令牌，出口由 auth-server 把 `Authorization` 换成它；`PATCH /api/servers/<path>/auth-credential`）→ ② 线侧 Secret `fd-legal-search-line-token` 注入 `MCP_BEARER_TOKEN` 并滚版（本线镜像 `sha-87f4fe9`）。**实测判据**（历次同形）：匿名直连 **401** `{"error":"unauthorized"}`、假令牌 **401**、真令牌 **200** 且拿到 MCP session；探针路径 /health,/healthz 豁免（不带令牌仍 200）；注册表行 `scheme=bearer enabled=true health=healthy tools=10`；经网关 initialize → 200（admin JWT）。**这正是 fallback 轨**：摘掉平台后，持有该令牌的客户仍能直接消费本线。⚠️ 共享钥匙**不承载客户身份**——客户级隔离/配额仍要靠平台（词元轨，见 `ciyuan-sk` 那条的 migration_task）。

### `auth_tracks[]`

未接：本仓不解析入站凭据。

### `billing`

本仓零计费代码（grep `sub2api` 无命中）：按调用扣费发生在网关/registry 的词元平面，不在本线代码里。只读检索；嵌入开销由本线出站凭据承担。
