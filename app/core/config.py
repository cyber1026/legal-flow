from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 开发默认 JWT 密钥；生产（environment=production）必须覆盖，否则启动即报错。
_DEFAULT_JWT_SECRET = "dev-jwt-signing-key-change-before-production"


class Settings(BaseSettings):
    """Centralized application settings loaded from `.env`."""

    app_name: str = "Legal Flow"
    # 运行环境；为 production/prod 时强制校验安全相关配置（见 _enforce_secure_jwt_secret）。
    environment: str = "development"

    google_api_key: str = ""

    milvus_uri: str = "http://localhost:19530"

    embedding_base_url: str = "http://localhost:7997"
    embedding_model: str = "models/bge-m3"
    embedding_dim: int = 1024

    llm_provider: Literal["gemini", "deepseek", "zhipuai"] = "deepseek"
    llm_temperature: float = 0.2
    # LLM 客户端超时与重试（流式下 timeout 是「单次读」级——即两次 token 之间的最大空闲，
    # 而非整条流的总时长）。上游建立连接后迟迟不吐 token 时据此抛错，避免流式调用永久挂起
    # 把上层（如合同审查 fan-in）拖死。
    llm_request_timeout: float = 120.0
    # 合同审查专用 chunk-idle 超时：review agent 需要长 reasoning（特别是 DeepSeek 在
    # 大输入下首 chunk 延迟显著），120s 容易在条款首批并发时被打穿。240s 给 reasoning
    # 留够余地；chat 不受影响仍用 llm_request_timeout。
    llm_review_timeout: float = 240.0
    llm_max_retries: int = 2

    google_model: str = "gemini-3-flash-preview"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_beta_base_url: str = "https://api.deepseek.com/beta"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_enable_thinking: bool = True

    zhipuai_api_key: str = ""
    zhipuai_base_url: str = "https://open.bigmodel.cn/api/paas/v4/"
    zhipuai_model: str = "glm-4.6v-flash"
    zhipuai_enable_thinking: bool = True

    # Query Rewriter — 独立于主 LLM，可指定更快的模型做查询改写
    rewriter_provider: str = ""
    rewriter_model: str = ""
    rewriter_min_length: int = 8

    top_k: int = 5

    # 法律知识库
    law_collection_name: str = "law_chunks"
    law_raw_dir: str = "data/raw"
    law_parsed_dir: str = "data/parsed_chunks"

    # 审查支撑知识库（第 2–4 层语料：司法解释/案例/playbook/标准合同条款·全文）
    # 5 个 collection 名定义在 app/knowledge/registry.py 的 KBSpec 里（单一事实来源），不散落 config。
    kb_chunks_dir: str = "data/legal_sources/_normalized/chunks"  # 归一化 chunk jsonl 目录
    kb_summarize_model: str = ""        # 检索结果总结用的快模型名；空=用当前 provider 默认模型
    kb_retrieve_top_k: int = 5         # 每个支撑库最终返回条数
    kb_retrieve_fetch_k: int = 20       # 向量层 over-fetch 条数（Python 内按 retrieval_weight 重排后截 top_k）
    kb_retrieve_timeout: float = 30.0   # 单次支撑库向量检索超时（含 embedding + Milvus）
    kb_summarize_timeout: float = 45.0  # 单次支撑库检索结果总结超时
    # 整份示范合同逐字全文（从磁盘回取）单次返回的字符上限：覆盖全部 595 份（最长约 9.2 万字），
    # 同时防止把超大全文一次性灌爆 LLM 上下文。超出部分提示去 source_url 看原件。
    kb_fulltext_max_chars: int = 120000

    # 合同智能审查
    contract_collection_name: str = "contract_chunks"
    contract_raw_dir: str = "data/contracts/raw"
    contract_max_clauses: int = 200       # 单合同条款上限，防止 LLM 调用失控
    review_concurrency: int = 4           # 并发审查条款数（避免 LLM rate limit）
    review_skip_boilerplate: bool = False # 开启后跳过被分类为「样板条款」的条款，不送审查 agent
    contract_max_upload_mb: int = 20      # 上传文件大小上限（MB）
    paddleocr_lang: str = "ch"
    paddleocr_use_gpu: bool = True
    # Docling 模型本地目录（layout/tableformer/RapidOCR 等），由
    # `docling.utils.model_downloader.download_models` 预下载到此处；设置后离线加载、不再联网拉取。
    docling_artifacts_path: str = "ckpts/docling_artifacts"

    # 业务关系库（用户、会话、消息） — PostgreSQL DSN
    database_url: str = "postgresql://legal_flow:change-me-local-postgres-password@localhost:5432/legal_flow"
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10
    # LangGraph checkpointer（立场 HITL）专用连接池上限
    checkpointer_pool_max_size: int = 10

    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7

    cors_origins: list[str] = ["http://localhost:3000", "http://127.0.0.1:3000"]

    # 日志系统
    log_level: str = "INFO"          # root logger 级别：DEBUG/INFO/WARNING/ERROR
    log_dir: str = "logs"            # 日志文件目录（相对项目根）
    log_to_file: bool = True         # 是否持久化到滚动文件
    log_file_max_mb: int = 10        # 单个日志文件大小上限（MB），超过滚动
    log_file_backup_count: int = 5   # 保留的历史日志文件个数

    # LangSmith 可观测性（tracing）。默认关闭：不配 key 也能正常跑；
    # 开启只需 .env 里设 langsmith_tracing=true + langsmith_api_key。
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "legal-flow"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def uses_default_jwt_secret(self) -> bool:
        """是否仍在使用开发默认 JWT 密钥（供启动期告警）。"""
        return self.jwt_secret == _DEFAULT_JWT_SECRET

    @model_validator(mode="after")
    def _enforce_secure_jwt_secret(self) -> "Settings":
        """生产环境禁止沿用开发默认 JWT 密钥（fail-fast，避免令牌被伪造）。"""
        if (
            self.environment.strip().lower() in {"production", "prod"}
            and self.uses_default_jwt_secret
        ):
            raise ValueError(
                "生产环境（environment=production）必须设置非默认的 JWT_SECRET，"
                "当前仍为开发默认值——请在环境变量/.env 配置强随机密钥后再启动。"
            )
        return self


settings = Settings()
