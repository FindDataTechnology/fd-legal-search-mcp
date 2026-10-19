"""工作流级知识（mcp-surface-contract 组 7）：以 MCP 资源随服务暴露。

工具描述回答"这个工具怎么用"，工作流知识回答"这几个工具按什么顺序串、中间
的形状契约是什么"。后者塞进工具描述会让描述变成一篇文档，而描述会被**每个**
调用方读进上下文（代价按调用次数线性放大）；资源则是需要时才拉取一次。

三份资源覆盖本服务的三类真实用法，每份都写清步骤顺序、跨步形状契约与易错点。

**为什么 URI 用服务名打头**：资源 URI 是全局命名空间，两个服务都叫
`workflow/...` 会撞名，客户端枚举时看不出是谁的。
"""
from __future__ import annotations

CASE_RESEARCH = """# 类案检索闭环（多步组合）

## 顺序与形状契约

1. **`wenshu_search(title_query, doc_type?, province?, start_year?, end_year?)`
   定位候选**
   - 案号内嵌于标题（如「（2024）京01民初1号」），无需单独案号参数
   - 输出 `results[].doc_id` 与 `has_body`；`has_body=false` 表示正文未入库
   - `truncated=true` 时结果被截断：用 `start_year/end_year` 收窄，而不是加大
     limit（limit 有 200 上限，且宽泛词命中本身就是海量的）

2. **`wenshu_read(doc_id, offset?, max_chars?)` 取正文**
   - 把第 1 步的 `doc_id` 传进来
   - 长正文按窗口返回：`content_truncated=true` 时把它给的 `next_offset`
     作为下一次的 `offset`，循环直到 `content_truncated=false`
   - **分段按字符切分，拼接与原文逐字符一致**——可以直接拼接后交给下游

3. **需要法条时走 `law_search` / `law_read`，不要从文书正文里抠法条名**
   - 正文里的法条引用是自由文本，可能有简称、别名、旧版本名
   - `law_reference(citation)` 做条级引用解析：把「《民法典》第一百四十三条」
     解析到法规目录的条级定位；解析不了如实报 not_found，**不猜**

## 易错点

- **`has_body=false` 不是错误**：存量快照并非全量语料，部分文书只有元数据。
  此时 `wenshu_read` 会如实标注，绝不编造正文。
- **把 `truncated` 当"查完了"**：它是"还有更多"的信号，不是"就这些"。
- **`content` 键缺失**：正文不可得时该键**不存在**（不是空字符串），判定请用
  `content_available`。
"""

STATUTE_LOOKUP = """# 法条检索与条级引用（多步组合）

## 顺序与形状契约

1. **`law_search(title_query, category?, limit?)` 找法规**
   - 排序 exact > prefix > contains（不是简单的包含匹配）
   - `category` 接受同义拼写；结果逐条带 provenance
   - `results[].law_id` 喂给第 2 步

2. **`law_read(law_id, offset?, limit?)` 读条文**
   - 条文按窗口返回（默认 200 条，上限 500）；整部法规一次全量回传会让大法典
     独占上下文
   - `truncated=true` 时把它给的 `next_offset` 作为下一次的 `offset`
   - `total_chunks` **始终给出**，不要靠"这一页短了"判断结束（长度恰好整除时
     最后一页也是满的）

3. **引用解析用 `law_reference(citation)`**
   - 输入「《民法典》第一百四十三条」这类中文引用
   - 输出法规 + 条级定位；无法解析如实报 not_found，**绝不猜测**

4. **语义检索用 `law_semantic_search(query, top_k?)`**
   - 走 Qwen3-Embedding 查询向量 → Qdrant `law_chunks_v2`
   - 适合"我知道要找什么但不知道法规名"的情形；知道名字时第 1 步更准

## 易错点

- **用语义检索替代精确检索**：知道法规名时用 `law_search`（exact 排序），
  语义检索在精确名上反而可能把同名的下位法排到前面。
- **`law_read` 的 offset 越界**：会报 not_found 并点名实际条文数，不是回空串。
"""

CORPUS_RETRIEVAL = """# 五源语料检索（多步组合）

## 顺序与形状契约

1. **`legal_docs_search(title_query, source?, doc_type?, limit?)` 检索**
   - `source` 是**封闭五源词表**：ccdi-supervisory-regulations /
     gov-rules-library / mfa-treaty-library / party-regulation-library /
     rmfyalk-case-library
   - 传入词表外的值会被**拒绝并给出合法列表**，不会静默匹配成空——这是刻意的
   - `results[].id` 与 `has_content` 喂给第 2 步

2. **`legal_docs_read(doc_id, offset?, max_chars?)` 取正文**
   - 长正文按窗口返回（默认 12000 字符，上限 40000）；实测本语料最长
     177,139 字符，不分段会一次吃掉整个上下文预算
   - `content_truncated=true` 时用 `next_offset` 续读

3. **指导案例走另一对：`guide_cases_search` / `guide_cases_read`**
   - 五个源里"人民法院案例库"是裁判文书/案例，指导案例（最高法发布的参照性
     案例）在单独的 `guide_cases` 集合里，**不要混用**

## 易错点

- **`content` 键缺失**：正文未入库时该键**不存在**（不是空字符串）；判定请用
  `content_available`。
- **把 `has_content` 当成"一定能读到"**：它只说明库里有正文列，读取仍可能因
  后端不可得而标注 `content_available=false`。
- **`source` 拼写**：写错会被拒绝并给合法列表，这比静默返回空结果好处理。
"""

RESOURCES = {
    "fd-legal-search-mcp://workflow/case-research": ("类案检索闭环", CASE_RESEARCH),
    "fd-legal-search-mcp://workflow/statute-lookup": ("法条检索与条级引用", STATUTE_LOOKUP),
    "fd-legal-search-mcp://workflow/corpus-retrieval": ("五源语料检索", CORPUS_RETRIEVAL),
}
