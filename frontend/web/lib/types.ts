export type Role = "user" | "assistant" | "system" | "tool";

export interface Citation {
  index: number;
  source: string;
  page?: number | null;
  headings?: string | null;
  chunk_id?: string | null;
  doc_id?: string | null;
  content: string;
  // 法律特有扩展字段
  law_name?: string;
  article_no?: string;
  citation_text?: string;
  effective_date?: string;
}

export interface ToolCall {
  call_id: string;
  name: string;
  args: Record<string, unknown>;
  status: "running" | "done" | "error";
  agent?: string;
  result_preview?: string;
  citations?: Citation[];
  /** QueryRewriter-expanded query actually used for vector search. */
  rewritten?: string;
  startedAt: number;
  endedAt?: number;
  elapsed_ms?: number;
}

/**
 * One step in the assistant's reasoning timeline, captured in true arrival
 * order so the UI can render the ReAct loop (thinking ↔ tool calls)
 * interleaved rather than lumping all thinking together and all tools together.
 */
export type ReasoningStep =
  | { kind: "thinking"; text: string; agent?: string }
  | { kind: "tool"; call: ToolCall };

export interface ChatMessage {
  id: string;
  role: Role;
  content: string;
  thinking?: string;
  thinkingMs?: number;
  toolCalls?: ToolCall[];
  /** Ordered reasoning timeline (thinking segments + tool calls interleaved). */
  steps?: ReasoningStep[];
  citations?: Citation[];
  rewritten?: string;
  /** data URLs or remote URLs uploaded by the user for this turn. */
  images?: string[];
  status?: "streaming" | "done" | "error";
  createdAt: number;
}

export interface SessionItem {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** 关联的合同 id（会话内开启了合同审查时非空）。 */
  contract_id?: number | null;
}

export interface LawIngestJob {
  job_id: string;
  filename: string;
  law_name?: string | null;
  status: "pending" | "parsing" | "embedding" | "done" | "failed" | string;
  parsed_chunks?: number | null;
  embedded_chunks?: number | null;
  error?: string | null;
  started_at: string;
  finished_at?: string | null;
}

export interface LawItem {
  law_name: string;
  doc_id: string;
  chunk_count: number;
  effective_date: string;
  version: string;
  law_status: string;
}

export interface User {
  id: number;
  email: string;
  created_at: string;
}

// ---------------------------------------------------------------------------
// 合同审查
// ---------------------------------------------------------------------------

export type ContractJobStatus =
  | "pending"
  | "parsing"
  | "embedding"
  | "reviewing"
  | "done"
  | "failed";

export interface ContractSummary {
  id: number;
  session_id?: string | null;
  job_id: string;
  filename: string;
  title: string;
  doc_type: string;
  status: ContractJobStatus;
  parsed_clauses: number;
  risk_count: number;
  opinion_count?: number;
  error?: string | null;
  party_stance?: "甲方" | "乙方" | "中立" | "未知";
  started_at?: string | null;
  finished_at?: string | null;
  created_at: string;
}

export interface ContractClause {
  id: number;
  clause_id: string;
  section_path: string;
  clause_no: string;
  title: string;
  text: string;
  page_no?: number | null;
  bbox?: number[] | null;
  chunk_index: number;
  review_status?: "pending" | "reviewing" | "done" | "failed" | "skipped";
  review_has_risk?: boolean;
  review_has_opinion?: boolean;
  reasoning?: ReasoningStep[];
}

export interface ReviewCitation {
  law_name: string;
  article_no: string;
  citation_text: string;
  chunk_id: string;
  excerpt: string;
  verified: boolean;
}

export type RiskLevel = "none" | "low" | "medium" | "high" | "critical";

export interface ReviewOpinion {
  id?: number;
  clause_id_ref?: number;
  opinion_type: string;        // 疑问/说明/提醒/建议/警告
  review_dimension: string;    // 主体合格性/内容合法性/条款实用性/权益明确性/合同严谨性/表述精确性
  finding: string;
  recommendation: string;
  confidence?: number;
  citations: ReviewCitation[];
  clause_id?: string;          // 审查面板：条款级风险关联（_risk_to_dict 带出）
  created_at?: string;
}

export interface ClauseRiskAssessment {
  id?: number;
  clause_id_ref?: number;
  clause_id?: string;
  risk_level: RiskLevel;
  rationale: string;
  affected_party: string;
  confidence?: number;
  created_at?: string;
}

export interface ConsistencyOpinion {
  id?: number;
  opinion_type: string;
  review_dimension: string;
  finding: string;
  recommendation: string;
  related_clause_ids: string[];
  evidence_facts: string[];
  confidence?: number;
  created_at?: string;
}

export interface ConsistencyRiskAssessment {
  id?: number;
  risk_level: RiskLevel;
  rationale: string;
  affected_party: string;
  confidence?: number;
  created_at?: string;
}

export interface ContractReport {
  contract: ContractSummary;
  clauses: ContractClause[];
  opinions: ReviewOpinion[];
  clause_risk_assessments: ClauseRiskAssessment[];
  consistency_opinions: ConsistencyOpinion[];
  consistency_risk_assessment?: ConsistencyRiskAssessment | null;
}

/** 单条款在全量审查流中的实时状态（review agent 推理 + 命中风险）。 */
export interface ClauseReview {
  clause_id: string;
  clause_no: string;
  title: string;
  status: "pending" | "reviewing" | "done" | "failed" | "skipped";
  steps: ReasoningStep[];
  opinions: ReviewOpinion[];
  hasOpinion: boolean;
  riskAssessment?: ClauseRiskAssessment | null;
  /** classify_clauses 给出的条款类别（如「核心义务」「样板条款」）。 */
  category?: string;
  /** 自动审查失败，需人工复核。 */
  failed?: boolean;
  /** 被判为样板条款而跳过审查。 */
  skipped?: boolean;
}

export type SSEEventName =
  // 对话流（/chat）
  | "session"
  | "rewrite"
  | "tool_call_start"
  | "tool_call_end"
  | "think_delta"
  | "answer_delta"
  // supervisor 顶层图 enqueue_review 节点触发的后台合同审查信号；前端据此打开审查 SSE。
  | "review_started"
  // ensure_stance 节点 interrupt：询问委托人立场，前端渲染立场选择卡片，选后以 resume 重入 /chat。
  | "stance_required"
  | "done"
  | "error"
  // 审查流（/contract-review/contracts/{id}/stream）
  | "status"
  | "clause_start"
  | "clause_think_delta"
  | "clause_tool_start"
  | "clause_tool_end"
  | "clause_done"
  | "consistency_start"
  | "consistency_delta"
  | "consistency_think_delta"
  | "consistency_done"
  // 条款级审查全部落库后触发：前端据此预取报告，但展示需等一致性审查结束。
  | "report_ready"
  | "review_not_started"
  | "overview_start"
  | "overview_think_delta"
  | "overview_delta"
  | "overview_done";

export interface SSEEvent {
  event: SSEEventName;
  data: Record<string, unknown>;
}
