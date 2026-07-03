# 贡献指南

感谢你关注 Legal Flow。这个项目目标是做可落地的中文法律 AI 系统，因此贡献应尽量围绕真实链路、可复现数据和可验证结果展开。

## 开发环境

```bash
uv sync
cd frontend/web
npm install
```

后端启动：

```bash
uv run uvicorn main:app --reload --reload-dir app --host 127.0.0.1 --port 8765
```

前端启动：

```bash
cd frontend/web
npm run dev
```

## 提交前检查

```bash
uv run pytest

cd frontend/web
npm run typecheck
npm run build
```

涉及 Milvus、PostgreSQL、embedding、OCR 或合同审查任务的改动，请在 PR 中说明依赖服务、数据来源和验证范围。

## 代码约定

- Python 使用 3.11，公共函数需要类型标注。
- 新增 Python 依赖优先使用 `uv add <package>`。
- 后端路由放在 `app/api/routes/`，DTO 放在 `app/api/schemas/`。
- 合同审查中不要臆测 `party_stance`，缺失时必须走 HITL。
- 审查意见与风险等级保持分离，不要把提示、疑问、建议混成风险等级。
- 对运行错误不要静默吞掉，至少写入日志。

## PR 建议

PR 描述请包含：

- 变更摘要
- 为什么需要这个变更
- 测试或验证结果
- 涉及的配置、迁移、外部服务或数据许可
- UI 改动截图

## 数据与安全

不要提交：

- `.env`、API Key、token、cookie
- `data/`、`reports/`、`volumes/`、`vector_store/`
- LLM 缓存、模型权重、运行日志
- 不能公开分发的合同、判例或法律语料
