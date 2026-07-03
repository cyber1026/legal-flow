# 数据库与向量库结构

本文档描述当前项目使用的两类数据存储：

- **PostgreSQL**：业务关系库，保存用户、会话、消息、合同审查结果等结构化数据。
- **Milvus**：向量数据库，保存法律条文与合同条款的 embedding 向量及检索 metadata。

结构来源：

- PostgreSQL DDL：`app/db.py`
- 法律向量入库：`app/ingest/law_ingest.py`
- 合同向量入库：`app/contracts/milvus_store.py`
- 配置项：`app/core/config.py`

---

## PostgreSQL 业务库

默认连接配置：

| 项 | 值 |
|---|---|
| 配置项 | `DATABASE_URL` |
| 默认 DSN | `postgresql://legal_flow:change-me-local-postgres-password@localhost:5432/legal_flow` |
| 初始化入口 | `app.db.init_schema()` |
| 建表方式 | 应用启动时执行幂等 DDL |

### PostgreSQL ER 图

```mermaid
erDiagram
    users ||--o{ sessions : "user_id"
    sessions ||--o{ messages : "session_id"
    users ||--o{ law_ingest_jobs : "user_id"
    users ||--o{ contracts : "user_id"
    sessions ||--o{ contracts : "session_id"
    contracts ||--o{ contract_clauses : "contract_id"
    contracts ||--o{ risk_items : "contract_id"
    contract_clauses ||--o{ risk_items : "clause_id_ref"
    risk_items ||--o{ risk_citations : "risk_id"

    users {
        BIGSERIAL id PK
        TEXT email UK
        TEXT password_hash
        TIMESTAMPTZ created_at
    }

    sessions {
        TEXT id PK
        BIGINT user_id FK
        TEXT title
        TIMESTAMPTZ created_at
        TIMESTAMPTZ updated_at
    }

    messages {
        BIGSERIAL id PK
        TEXT session_id FK
        TEXT role
        TEXT content
        TEXT citations_json
        TEXT tool_calls_json
        TEXT thinking
        INTEGER thinking_ms
        TEXT reasoning_json
        TEXT images_json
        TIMESTAMPTZ created_at
    }

    law_ingest_jobs {
        TEXT job_id PK
        BIGINT user_id FK
        TEXT filename
        TEXT law_name
        TEXT status
        INTEGER parsed_chunks
        INTEGER embedded_chunks
        TEXT error
        TIMESTAMPTZ started_at
        TIMESTAMPTZ finished_at
    }

    contracts {
        BIGSERIAL id PK
        BIGINT user_id FK
        TEXT session_id FK
        TEXT job_id UK
        TEXT filename
        TEXT mime
        TEXT doc_type
        TEXT storage_path
        TEXT title
        TEXT status
        INTEGER parsed_clauses
        INTEGER risk_count
        TEXT error
        TIMESTAMPTZ started_at
        TIMESTAMPTZ finished_at
        TIMESTAMPTZ created_at
    }

    contract_clauses {
        BIGSERIAL id PK
        BIGINT contract_id FK
        TEXT clause_id
        TEXT section_path
        TEXT clause_no
        TEXT title
        TEXT text
        INTEGER page_no
        TEXT bbox_json
        INTEGER chunk_index
        TEXT review_status
        BOOLEAN review_has_risk
        TEXT reasoning_json
        TIMESTAMPTZ created_at
    }

    risk_items {
        BIGSERIAL id PK
        BIGINT contract_id FK
        BIGINT clause_id_ref FK
        TEXT risk_type
        TEXT risk_level
        TEXT description
        TEXT suggestion
        REAL confidence
        TIMESTAMPTZ created_at
    }

    risk_citations {
        BIGSERIAL id PK
        BIGINT risk_id FK
        TEXT law_name
        TEXT article_no
        TEXT citation_text
        TEXT chunk_id
        TEXT excerpt
        BOOLEAN verified
    }
```

### `users`

保存注册用户账号。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PRIMARY KEY | 用户 ID。 |
| `email` | `TEXT` | NOT NULL, UNIQUE | 登录邮箱。 |
| `password_hash` | `TEXT` | NOT NULL | 密码哈希，不保存明文。 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 注册时间。 |

### `sessions`

保存聊天会话。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `TEXT` | PRIMARY KEY | 会话 ID。 |
| `user_id` | `BIGINT` | NOT NULL, FK -> `users(id)` ON DELETE CASCADE | 所属用户。 |
| `title` | `TEXT` | NOT NULL, DEFAULT `'新会话'` | 会话标题。 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 创建时间。 |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 最后更新时间。 |

索引：

| 索引 | 字段 |
|---|---|
| `idx_sessions_user_id` | `user_id` |

### `messages`

保存聊天消息历史。页面刷新后恢复对话主要依赖此表。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PRIMARY KEY | 消息 ID。 |
| `session_id` | `TEXT` | NOT NULL, FK -> `sessions(id)` ON DELETE CASCADE | 所属会话。 |
| `role` | `TEXT` | NOT NULL, CHECK IN (`user`,`assistant`,`system`,`tool`) | 消息角色。 |
| `content` | `TEXT` | NOT NULL | 消息正文。 |
| `citations_json` | `TEXT` | 可空 | 引用来源，JSON 字符串。 |
| `tool_calls_json` | `TEXT` | 可空 | 工具调用记录，JSON 字符串。 |
| `thinking` | `TEXT` | 可空 | 模型思考内容。 |
| `thinking_ms` | `INTEGER` | 可空 | 思考耗时，单位毫秒。 |
| `reasoning_json` | `TEXT` | 可空 | 思考与工具调用交替时间线，JSON 字符串。 |
| `images_json` | `TEXT` | 可空 | 用户上传图片列表，JSON 字符串。 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 消息创建时间。 |

索引：

| 索引 | 字段 |
|---|---|
| `idx_messages_session_id` | `session_id` |

### `law_ingest_jobs`

保存法律文件入库任务状态。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `job_id` | `TEXT` | PRIMARY KEY | 入库任务 ID。 |
| `user_id` | `BIGINT` | NOT NULL, FK -> `users(id)` ON DELETE CASCADE | 发起任务的用户。 |
| `filename` | `TEXT` | NOT NULL | 上传文件名。 |
| `law_name` | `TEXT` | 可空 | 法律名称。 |
| `status` | `TEXT` | NOT NULL, CHECK IN (`pending`,`parsing`,`embedding`,`done`,`failed`) | 入库状态。 |
| `parsed_chunks` | `INTEGER` | 可空 | 已解析 chunk 数。 |
| `embedded_chunks` | `INTEGER` | 可空 | 已写入 Milvus 的 chunk 数。 |
| `error` | `TEXT` | 可空 | 失败错误信息。 |
| `started_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 开始时间。 |
| `finished_at` | `TIMESTAMPTZ` | 可空 | 结束时间。 |

索引：

| 索引 | 字段 |
|---|---|
| `idx_law_ingest_jobs_user_id` | `user_id` |
| `idx_law_ingest_jobs_started_at` | `started_at DESC` |

### `contracts`

保存合同上传记录和合同审查任务主状态。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PRIMARY KEY | 合同 ID。 |
| `user_id` | `BIGINT` | NOT NULL, FK -> `users(id)` ON DELETE CASCADE | 上传用户。 |
| `session_id` | `TEXT` | FK -> `sessions(id)` ON DELETE CASCADE | 关联合同审查会话。 |
| `job_id` | `TEXT` | NOT NULL, UNIQUE | 合同审查 job ID。 |
| `filename` | `TEXT` | NOT NULL | 原始文件名。 |
| `mime` | `TEXT` | NOT NULL, DEFAULT `''` | MIME 类型。 |
| `doc_type` | `TEXT` | NOT NULL, CHECK IN (`image`,`pdf`,`docx`) | 文档类型。 |
| `storage_path` | `TEXT` | NOT NULL | 原始文件落盘路径。 |
| `title` | `TEXT` | NOT NULL, DEFAULT `''` | 解析出的合同标题。 |
| `status` | `TEXT` | NOT NULL, DEFAULT `pending`, CHECK IN (`pending`,`parsing`,`embedding`,`reviewing`,`done`,`failed`) | 审查任务状态。 |
| `parsed_clauses` | `INTEGER` | NOT NULL, DEFAULT `0` | 已解析条款数。 |
| `risk_count` | `INTEGER` | NOT NULL, DEFAULT `0` | 风险数量。 |
| `error` | `TEXT` | 可空 | 失败错误信息。 |
| `started_at` | `TIMESTAMPTZ` | 可空 | 任务开始时间。 |
| `finished_at` | `TIMESTAMPTZ` | 可空 | 任务结束时间。 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 上传记录创建时间。 |

索引：

| 索引 | 字段 |
|---|---|
| `idx_contracts_user_id` | `user_id` |
| `idx_contracts_session_id` | `session_id` |
| `idx_contracts_job_id` | `job_id` |

### `contract_clauses`

保存合同解析后拆分出的条款。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PRIMARY KEY | 条款数据库 ID。 |
| `contract_id` | `BIGINT` | NOT NULL, FK -> `contracts(id)` ON DELETE CASCADE | 所属合同。 |
| `clause_id` | `TEXT` | NOT NULL | 条款业务 ID。 |
| `section_path` | `TEXT` | NOT NULL, DEFAULT `''` | 条款章节路径。 |
| `clause_no` | `TEXT` | NOT NULL, DEFAULT `''` | 条款编号。 |
| `title` | `TEXT` | NOT NULL, DEFAULT `''` | 条款标题。 |
| `text` | `TEXT` | NOT NULL | 条款正文。 |
| `page_no` | `INTEGER` | 可空 | 页码。 |
| `bbox_json` | `TEXT` | 可空 | 页面坐标，JSON 字符串。 |
| `chunk_index` | `INTEGER` | NOT NULL, DEFAULT `0` | 条款顺序。 |
| `review_status` | `TEXT` | NOT NULL, DEFAULT `pending`, CHECK IN (`pending`,`reviewing`,`done`) | 条款审查状态。 |
| `review_has_risk` | `BOOLEAN` | NOT NULL, DEFAULT `false` | 条款是否命中风险。 |
| `reasoning_json` | `TEXT` | 可空 | 条款审查推理过程，JSON 字符串。 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 条款创建时间。 |

索引与唯一约束：

| 名称 | 字段 |
|---|---|
| `idx_clauses_contract_id` | `contract_id` |
| `uq_contract_clauses_contract_clause` | UNIQUE (`contract_id`, `clause_id`) |

### `risk_items`

保存合同审查发现的风险项。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PRIMARY KEY | 风险项 ID。 |
| `contract_id` | `BIGINT` | NOT NULL, FK -> `contracts(id)` ON DELETE CASCADE | 所属合同。 |
| `clause_id_ref` | `BIGINT` | NOT NULL, FK -> `contract_clauses(id)` ON DELETE CASCADE | 风险对应条款。 |
| `risk_type` | `TEXT` | NOT NULL, DEFAULT `'其他'` | 风险类型。 |
| `risk_level` | `TEXT` | NOT NULL, CHECK IN (`low`,`medium`,`high`,`critical`) | 风险等级。 |
| `description` | `TEXT` | NOT NULL, DEFAULT `''` | 风险说明。 |
| `suggestion` | `TEXT` | NOT NULL, DEFAULT `''` | 修改建议。 |
| `confidence` | `REAL` | NOT NULL, DEFAULT `0` | 置信度。 |
| `created_at` | `TIMESTAMPTZ` | NOT NULL, DEFAULT `NOW()` | 风险创建时间。 |

索引：

| 索引 | 字段 |
|---|---|
| `idx_risks_contract_id` | `contract_id` |
| `idx_risks_clause_id_ref` | `clause_id_ref` |

### `risk_citations`

保存风险项引用的法律依据。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | `BIGSERIAL` | PRIMARY KEY | 引用 ID。 |
| `risk_id` | `BIGINT` | NOT NULL, FK -> `risk_items(id)` ON DELETE CASCADE | 所属风险项。 |
| `law_name` | `TEXT` | NOT NULL, DEFAULT `''` | 法律名称。 |
| `article_no` | `TEXT` | NOT NULL, DEFAULT `''` | 条文号。 |
| `citation_text` | `TEXT` | NOT NULL, DEFAULT `''` | 引用文本。 |
| `chunk_id` | `TEXT` | NOT NULL, DEFAULT `''` | 对应 `law_chunks.chunk_id`。 |
| `excerpt` | `TEXT` | NOT NULL, DEFAULT `''` | 法条摘录。 |
| `verified` | `BOOLEAN` | NOT NULL, DEFAULT `false` | 是否经本地法库核验。 |

索引与唯一约束：

| 名称 | 字段 |
|---|---|
| `idx_citations_risk_id` | `risk_id` |
| `uq_risk_citations_risk_article` | UNIQUE (`risk_id`, `law_name`, `article_no`) |

---

## Milvus 向量数据库

默认连接配置：

| 项 | 值 |
|---|---|
| 配置项 | `MILVUS_URI` |
| 默认 URI | `http://localhost:19530` |
| embedding 模型 | `models/bge-m3` |
| embedding 维度 | `1024` |
| 索引类型 | `HNSW` |
| 距离度量 | `COSINE` |
| 检索参数 | `ef = 64` |
| 写入方式 | `langchain_milvus.Milvus.add_documents()` |

当前有两个 collection：

| Collection | 配置项 | 用途 |
|---|---|---|
| `law_chunks` | `LAW_COLLECTION_NAME` | 法律法规条文向量库。 |
| `contract_chunks` | `CONTRACT_COLLECTION_NAME` | 合同条款向量库。 |

### Milvus Collection 总览

```mermaid
classDiagram
    class law_chunks {
        pk: auto_id primary key
        text: embedding_text
        vector: float[1024]
        article_text
        chunk_id
        doc_id
        doc_type
        law_name
        part
        chapter
        section
        parent_path
        article_no
        keywords
        citation_text
        status
        effective_date
        version
    }

    class contract_chunks {
        pk: auto_id primary key
        text: embedding_text
        vector: float[1024]
        chunk_id
        contract_id
        user_id
        doc_type
        status
        contract_title
        section_path
        clause_no
        title
        clause_text
        page_no
        bbox
    }
```

> 说明：两个 collection 都启用了 `enable_dynamic_field=True`。因此除 `pk`、`text`、`vector` 外，metadata 字段作为 Milvus dynamic fields 写入，并可用于查询过滤。

### `law_chunks`

保存法律法规条文 chunk 的向量。

基础字段：

| 字段 | 类型/来源 | 说明 |
|---|---|---|
| `pk` | Milvus auto id | 主键，自动生成。 |
| `text` | `Document.page_content` | 用于 embedding 的文本，即 `embedding_text`。 |
| `vector` | float vector, 1024 维 | `text` 的向量。 |

动态 metadata 字段：

| 字段 | 来源 | 说明 |
|---|---|---|
| `article_text` | `chunk["text"]` | 原始法条正文。 |
| `chunk_id` | `chunk["chunk_id"]` | 法条 chunk ID。 |
| `doc_id` | `chunk["doc_id"]` | 法律文档 ID。 |
| `doc_type` | `chunk["doc_type"]`，默认 `law` | 文档类型。 |
| `law_name` | `chunk["law_name"]` | 法律名称。 |
| `part` | `chunk["part"]` | 编。 |
| `chapter` | `chunk["chapter"]` | 章。 |
| `section` | `chunk["section"]` | 节。 |
| `parent_path` | `chunk["parent_path"]` JSON 字符串 | 层级路径。 |
| `article_no` | `chunk["article_no"]` | 条文号。 |
| `keywords` | `chunk["keywords"]` JSON 字符串 | 关键词。 |
| `citation_text` | `chunk["citation_text"]` | 标准引用文本。 |
| `status` | `chunk["status"]`，默认 `effective` | 法律状态。 |
| `effective_date` | `chunk["effective_date"]` | 生效日期。 |
| `version` | `chunk["version"]` | 版本。 |

常见查询：

| 场景 | 查询方式 |
|---|---|
| 语义检索 | `similarity_search_with_score(query, k=top_k)` |
| 按法律过滤 | `expr='law_name == "..."'` |
| 精确查条文 | `expr='law_name == "..." and article_no == "..."'` |
| 删除某部法律 | `expr='law_name == "..."'` |

### `contract_chunks`

保存合同条款 chunk 的向量。

基础字段：

| 字段 | 类型/来源 | 说明 |
|---|---|---|
| `pk` | Milvus auto id | 主键，自动生成。 |
| `text` | `Document.page_content` | 用于 embedding 的合同条款上下文文本。 |
| `vector` | float vector, 1024 维 | `text` 的向量。 |

`text` 构造模板：

```text
合同：{contract_title}

章节：{section_path}

条款：{clause_no} {clause_title}

{clause_text}
```

动态 metadata 字段：

| 字段 | 来源 | 说明 |
|---|---|---|
| `chunk_id` | 参数 `chunk_id` | 合同条款 chunk ID。 |
| `contract_id` | 参数 `contract_id` | 对应 PostgreSQL `contracts.id`。 |
| `user_id` | 参数 `user_id` | 所属用户。 |
| `doc_type` | 固定 `contract` | 文档类型。 |
| `status` | 固定 `active` | chunk 状态。 |
| `contract_title` | 参数 `contract_title` | 合同标题。 |
| `section_path` | 参数 `section_path` | 条款章节路径。 |
| `clause_no` | 参数 `clause_no` | 条款编号。 |
| `title` | 参数 `clause_title` | 条款标题。 |
| `clause_text` | 参数 `clause_text` | 条款原文。 |
| `page_no` | 参数 `page_no`，空值写 `-1` | 页码。 |
| `bbox` | 参数 `bbox` JSON 字符串 | 页面坐标。 |

常见查询：

| 场景 | 查询方式 |
|---|---|
| 合同条款语义检索 | `similarity_search_with_score(query, expr='contract_id == ...')` |
| 删除某份合同向量 | `expr='contract_id == {contract_id}'` |

---

## 数据边界

| 数据 | PostgreSQL | Milvus | 本地文件 |
|---|---|---|---|
| 用户账号 | 是 | 否 | 否 |
| 聊天会话与消息 | 是 | 否 | 否 |
| 聊天引用与工具调用记录 | 是，JSON 字符串 | 否 | 否 |
| 法律入库任务状态 | 是 | 否 | 否 |
| 法律条文向量 | 否 | `law_chunks` | 否 |
| 法律解析 JSONL | 否 | 否 | `data/parsed_chunks` |
| 合同上传记录 | 是 | 否 | 否 |
| 合同原始文件 | 路径在 PG | 否 | `data/contracts/raw` |
| 合同条款结构化内容 | 是 | `contract_chunks` 保存向量副本 | 否 |
| 合同风险与引用 | 是 | 否 | 否 |
