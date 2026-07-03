# 法律 RAG Chunk Schema（精简版）

## 一、推荐 Schema 字段表


| 字段               | 含义             | 示例                     | 是否参与 Embedding | 是否必须 | 主要用途            | 获取方式                   |
| ---------------- | -------------- | ---------------------- | -------------- | ---- | --------------- | ---------------------- |
| `chunk_id`       | 当前 chunk 唯一 ID | `law_data_security_21` | ❌              | ✅    | 主键、向量库ID        | 系统生成（UUID/规则拼接）        |
| `doc_id`         | 所属法律文档 ID      | `law_data_security`    | ❌              | ✅    | 文档归属            | 系统生成                   |
| `doc_type`       | 文档类型           | `law`                  | ❌              | ✅    | metadata filter | 固定枚举值                  |
| `law_name`       | 法律名称           | `中华人民共和国数据安全法`         | ✅              | ✅    | 检索、过滤、引用        | 文档解析（标题抽取）             |
| `part`           | 编              | `合同编`                  | ⚠️             | ⚠️   | 法律层级语义          | 正则 + 文档解析              |
| `chapter`        | 章              | `第三章 数据安全制度`           | ✅              | ✅    | 层级检索            | 正则 + 文档解析              |
| `section`        | 节              | `第一节 一般规定`             | ⚠️             | ⚠️   | 细粒度层级           | 正则 + 文档解析              |
| `parent_path`    | 完整层级路径         | `[数据安全法, 第三章]`         | ✅              | ✅    | 上下文语义           | 系统根据层级拼接               |
| `article_no`     | 法条编号           | `第二十一条`                | ✅              | ✅    | 法条定位、Citation   | 正则抽取                   |
| `text`           | 法条原文           | `国家建立数据分类分级保护制度...`    | ✅              | ✅    | 核心法律内容          | 文档解析                   |
| `embedding_text` | 真正用于向量化的文本     | 拼接后的检索文本               | ✅（核心字段）        | ✅    | Dense Retrieval | 系统模板拼接                 |
| `keywords`       | 关键词            | `["重要数据","数据安全"]`      | ⚠️ 可选          | ⚠️   | BM25增强          | TF-IDF / KeyBERT / LLM |
| `citation_text`  | 标准引用文本         | `《数据安全法》第二十一条`         | ❌              | ✅    | 法条引用展示          | 系统模板拼接                 |
| `status`         | 法律状态           | `effective`            | ❌              | ✅    | 过滤失效法律          | 默认值 + 外部法律更新           |
| `effective_date` | 生效日期           | `2021-09-01`           | ❌              | ✅    | 时间有效性           | 正则抽取                   |
| `version`        | 法律版本           | `20210610`             | ❌              | ✅    | 法律更新管理          | 文件名 / 发布时间解析           |


---

# 二、字段获取流程

## 1. 文档解析阶段

输入：

```text
DOCX / PDF
```

首先需要：

- 提取纯文本
- 保留法律层级结构
- 保留法条编号

这一阶段主要获取：


| 获取字段             |
| ---------------- |
| `law_name`       |
| `part`           |
| `chapter`        |
| `section`        |
| `article_no`     |
| `text`           |
| `effective_date` |


推荐工具：

- python-docx
- unstructured
- pymupdf
- docling

---

## 2. 法律结构解析阶段

核心目标：

```text
一个 chunk = 一个法条
```

例如：

```text
第三章 数据安全制度
第二十一条 国家建立数据分类分级保护制度...
```

解析后：

```json
{
  "chapter": "第三章 数据安全制度",
  "article_no": "第二十一条"
}
```

这一阶段主要生成：


| 获取字段          |
| ------------- |
| `parent_path` |
| `chunk_id`    |
| `doc_id`      |


---

## 3. Embedding 文本构建阶段

真正送去 embedding 的：

```text
中华人民共和国数据安全法

第三章 数据安全制度

第二十一条

国家建立数据分类分级保护制度...
```

因此：

需要构造：

```text
embedding_text
```

推荐模板：

```python
embedding_text = f'''
{law_name}

{part}

{chapter}

{section}

{article_no}

{text}
'''
```

---

## 4. NLP 增强阶段

跳过

---

## 5. 系统元数据生成阶段

这一阶段：

生成系统管理字段。

包括：


| 获取字段            |
| --------------- |
| `citation_text` |
| `status`        |
| `version`       |


例如：

```text
《中华人民共和国数据安全法》第二十一条
```

可以通过模板拼接生成。

---

# 三、推荐的 Parser Pipeline

推荐整体流程：

```text
DOCX/PDF
 ↓
文本提取
 ↓
法律层级识别
 ↓
法条切分
 ↓
metadata构建
 ↓
embedding_text构建
 ↓
向量化
 ↓
入库
```

---

# 四、一些重要建议

---

## 1. 法律最小语义单位是“条”

不要：

```text
512 token 随机切分
```

而应该：

```text
一个 chunk = 一个法条
```

否则：

会破坏法律语义。

---

## 3. 法律层级一定要保留

不要只保存：

```json
{
  "article_no": "",
  "text": ""
}
```

还必须保留：

```json
{
  "part": "",
  "chapter": "",
  "section": ""
}
```

因为：

法律层级本身带有重要语义。

---

## 4. Hybrid Retrieval 非常重要

法律系统：

特别适合：

```text
BM25 + Dense Retrieval
```

因为法律存在大量：

- 精确术语
- 法条编号
- 固定表达

仅 Dense Retrieval 效果通常不够好。

---

## 5. 不要一开始就做 GraphRAG

推荐路线：

第一阶段：

```text
结构化法律 Chunk
+
Hybrid Retrieval
+
Citation
```

第二阶段：

```text
风险规则库
```

第三阶段：

```text
GraphRAG
```

这样开发复杂度最合理。

---

## 6. 版本管理一定要做

法律会更新。

必须保存：

```json
{
  "version": "",
  "effective_date": "",
  "status": ""
}
```

否则：

系统可能引用失效法条。

---

## 7. 法律、合同、案例不要共用同一个 Schema

推荐拆分：

```text
law_chunk
contract_chunk
case_chunk
```

因为三者结构完全不同。

共享：

```text
chunk_id
text
embedding_text
keywords
```

即可。