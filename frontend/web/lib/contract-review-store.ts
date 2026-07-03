"use client";

import { create } from "zustand";
import { apiFetch, apiJson } from "./api";
import { streamReview } from "./sse";
import { useChatStore } from "./chat-store";
import type {
  ClauseReview,
  ContractReport,
  ContractSummary,
  ContractJobStatus,
  ClauseRiskAssessment,
  ConsistencyOpinion,
  ConsistencyRiskAssessment,
  ReasoningStep,
  ReviewOpinion,
  SSEEvent,
  ToolCall,
} from "./types";

export type ReviewStatus = "idle" | "uploading" | "reviewing" | "done" | "failed";
type ContractTab = "clause" | "preview";

/** 合同级一致性审查的实时状态（审查流 consistency_* 事件 / 报告还原驱动）。 */
export interface ConsistencyState {
  /** pending：审查流一开始即占位展示，待条款审查完成后才真正开始。 */
  status: "pending" | "reviewing" | "done" | "failed";
  /** 进行中的提示文案（consistency_delta 推送）。 */
  message?: string;
  /** 一致性 agent 的推理过程（think 增量），与条款级审查同样可展开查看。 */
  steps: ReasoningStep[];
  hasOpinion: boolean;
  opinions: ConsistencyOpinion[];
  riskAssessment: ConsistencyRiskAssessment | null;
  note?: string;
  error?: string;
}

/** 单次合同审查运行的冻结快照；重新审查时用于保留旧过程气泡和旧报告气泡。 */
export interface ContractReviewRunSnapshot {
  id: string;
  contractId: number;
  timestamp: number;
  status: ReviewStatus;
  stage: ContractJobStatus | null;
  summary: ContractSummary | null;
  report: ContractReport | null;
  reportReady: boolean;
  clauseReviews: Record<string, ClauseReview>;
  clauseOrder: string[];
  consistency: ConsistencyState | null;
}

interface ContractReviewState {
  contractId: number | null;
  jobId: string | null;
  status: ReviewStatus;
  summary: ContractSummary | null;
  report: ContractReport | null;
  /** 已完成或被新一轮审查替换的历史审查运行。 */
  reviewRuns: ContractReviewRunSnapshot[];
  /** 当前审查运行 id；用于 React key 稳定地区分重审气泡。 */
  currentRunId: string | null;
  /** 当前审查运行的前端锚点时间，避免总览流式消息改变排序。 */
  currentRunStartedAt: number | null;
  /** 全量审查流中每条款的实时推理状态，按 clause_id 索引。 */
  clauseReviews: Record<string, ClauseReview>;
  clauseOrder: string[];
  /** 顶部阶段标签（parsing/embedding/reviewing），由审查流的 status 事件驱动。 */
  stage: ContractJobStatus | null;
  /** 审查报告是否可展示：必须在全文一致性审查结束后置真。 */
  reportReady: boolean;
  /** 已发出审查指令（点了「发送审查指令」或选了立场）但审查尚未真正开始——挂载气泡据此切换文案。 */
  instructionSent: boolean;
  /** 合同级一致性审查状态；null 表示本次审查尚未进入一致性阶段。 */
  consistency: ConsistencyState | null;
  activeClauseId: number | null;
  activeTab: ContractTab;
  /** 合同审查面板是否展开（关闭后会话退化为纯对话）。 */
  panelOpen: boolean;
  error: string | null;

  upload: (file: File, sessionId?: string) => Promise<ContractSummary>;
  streamReview: (contractId: number, opts?: { reset?: boolean }) => Promise<void>;
  setPartyStanceAndStart: (contractId: number, stance: string) => Promise<void>;
  rerun: () => void;
  stopReview: () => void;
  fetchReport: (contractId: number) => Promise<void>;
  loadContract: (contractId: number) => Promise<void>;
  setActiveClause: (id: number | null) => void;
  setActiveTab: (tab: ContractTab) => void;
  setPanelOpen: (open: boolean) => void;
  togglePanel: () => void;
  reset: () => void;
}

const TERMINAL_STATUSES = new Set<ContractJobStatus>(["done", "failed"]);

let reviewAbort: AbortController | null = null;
let activeReviewContractId: number | null = null;

/** 生成一次审查运行的本地 id；后端未提供 run_id，因此用合同 id + 时间保证前端 key 稳定。 */
function newReviewRunId(contractId: number) {
  return `review-${contractId}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

/** 解析后端时间为毫秒时间戳；解析失败时返回 null，避免污染时间线排序。 */
function toMaybeTimestamp(value: string | number | Date | null | undefined) {
  if (value == null) return null;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

/** 深拷贝当前条款审查状态，避免重审后旧气泡被后续增量事件改写。 */
function cloneClauseReviews(
  source: Record<string, ClauseReview>,
): Record<string, ClauseReview> {
  const cloned: Record<string, ClauseReview> = {};
  for (const [clauseId, review] of Object.entries(source)) {
    cloned[clauseId] = {
      ...review,
      steps: [...review.steps],
      opinions: [...review.opinions],
      riskAssessment: review.riskAssessment ? { ...review.riskAssessment } : review.riskAssessment,
    };
  }
  return cloned;
}

/** 深拷贝一致性审查状态，供历史运行快照独立展示。 */
function cloneConsistency(state: ConsistencyState | null): ConsistencyState | null {
  if (!state) return null;
  return {
    ...state,
    steps: [...state.steps],
    opinions: [...state.opinions],
    riskAssessment: state.riskAssessment ? { ...state.riskAssessment } : state.riskAssessment,
  };
}

/** 条款是否已经进入真实审查过程；仅 pending 的条款骨架不算。 */
function hasStartedClauseReview(state: ContractReviewState) {
  return state.clauseOrder.some((clauseId) => {
    const review = state.clauseReviews[clauseId];
    if (!review) return false;
    return (
      review.status !== "pending" ||
      review.steps.length > 0 ||
      review.opinions.length > 0 ||
      review.riskAssessment != null ||
      review.hasOpinion ||
      review.failed === true ||
      review.skipped === true
    );
  });
}

/** 一致性审查是否有真实过程或结果；pending 占位不算历史内容。 */
function hasStartedConsistencyReview(consistency: ConsistencyState | null) {
  if (!consistency || consistency.status === "pending") return false;
  return (
    consistency.status === "reviewing" ||
    consistency.status === "done" ||
    consistency.status === "failed" ||
    consistency.steps.length > 0 ||
    consistency.opinions.length > 0 ||
    consistency.riskAssessment != null ||
    Boolean(consistency.error)
  );
}

/** 当前运行是否有可保留内容；空上传态、立场确认前占位态和未启动态不写入历史。 */
function hasSnapshotContent(state: ContractReviewState) {
  const reportHasClauses = (state.report?.clauses.length ?? 0) > 0;
  const hasClauseStructure = state.clauseOrder.length > 0 || reportHasClauses;
  return (
    reportHasClauses ||
    (state.reportReady && hasClauseStructure) ||
    hasStartedClauseReview(state) ||
    (hasClauseStructure && hasStartedConsistencyReview(state.consistency))
  );
}

/** 把当前单例审查状态冻结成历史运行；重新审查时旧过程/旧报告都靠它保留。 */
function snapshotCurrentRun(state: ContractReviewState): ContractReviewRunSnapshot | null {
  if (state.contractId == null || !hasSnapshotContent(state)) return null;
  const summaryStartedAt = toMaybeTimestamp(state.summary?.started_at);
  const summaryCreatedAt = toMaybeTimestamp(state.summary?.created_at);
  const timestamp = state.currentRunStartedAt ?? summaryStartedAt ?? summaryCreatedAt ?? Date.now();
  const status: ReviewStatus =
    state.status === "failed"
      ? "failed"
      : state.status === "done" || state.reportReady || state.report?.contract.status === "done"
        ? "done"
        : "reviewing";
  return {
    id: state.currentRunId ?? newReviewRunId(state.contractId),
    contractId: state.contractId,
    timestamp,
    status,
    stage: state.stage,
    summary: state.summary ? { ...state.summary } : null,
    report: state.report,
    reportReady: state.reportReady,
    clauseReviews: cloneClauseReviews(state.clauseReviews),
    clauseOrder: [...state.clauseOrder],
    consistency: cloneConsistency(state.consistency),
  };
}

function normalizeReasoningStep(step: unknown): ReasoningStep | null {
  if (!step || typeof step !== "object") return null;
  const kind = (step as { kind?: unknown }).kind;
  if (kind === "thinking") {
    return {
      kind: "thinking",
      text: String((step as { text?: unknown }).text ?? ""),
      agent: typeof (step as { agent?: unknown }).agent === "string"
        ? String((step as { agent?: unknown }).agent)
        : undefined,
    };
  }
  // 旧报告里可能存有 kind="answer" 的历史步骤（content 通道已停止展示）：返回 null 丢弃即可。
  if (kind === "tool") {
    const rawCall = (step as { call?: Record<string, unknown> }).call ?? {};
    const startedAt = Number(rawCall.startedAt ?? Date.now());
    return {
      kind: "tool",
      call: {
        call_id: String(rawCall.call_id ?? ""),
        name: String(rawCall.name ?? "tool"),
        args: (rawCall.args as Record<string, unknown>) ?? {},
        status:
          rawCall.status === "running" || rawCall.status === "error" ? rawCall.status : "done",
        agent: typeof rawCall.agent === "string" ? rawCall.agent : undefined,
        result_preview:
          typeof rawCall.result_preview === "string" ? rawCall.result_preview : undefined,
        citations: (rawCall.citations as ToolCall["citations"]) ?? undefined,
        startedAt,
        endedAt: typeof rawCall.endedAt === "number" ? rawCall.endedAt : undefined,
        elapsed_ms:
          typeof rawCall.elapsed_ms === "number"
            ? rawCall.elapsed_ms
            : typeof rawCall.endedAt === "number"
              ? Math.max(0, rawCall.endedAt - startedAt)
              : undefined,
      },
    };
  }
  return null;
}

function restoreClauseReviews(report: ContractReport) {
  const clauseReviews: Record<string, ClauseReview> = {};
  const clauseOrder: string[] = [];
  for (const clause of report.clauses) {
    const opinions = report.opinions.filter((opinion) => opinion.clause_id_ref === clause.id);
    const riskAssessment =
      report.clause_risk_assessments.find((item) => item.clause_id_ref === clause.id) ?? null;
    const reviewStatus =
      clause.review_status ?? (report.contract.status === "done" ? "done" : "pending");
    clauseReviews[clause.clause_id] = {
      clause_id: clause.clause_id,
      clause_no: clause.clause_no,
      title: clause.title,
      status: reviewStatus,
      steps: (clause.reasoning ?? [])
        .map((step) => normalizeReasoningStep(step))
        .filter((step): step is ReasoningStep => step !== null),
      opinions,
      hasOpinion: clause.review_has_opinion ?? opinions.length > 0,
      riskAssessment,
      failed: reviewStatus === "failed",
      skipped: reviewStatus === "skipped",
    };
    clauseOrder.push(clause.clause_id);
  }
  return { clauseReviews, clauseOrder };
}

function isInProgress(status: ContractJobStatus) {
  return status === "parsing" || status === "embedding" || status === "reviewing";
}

/** 从已落库的报告还原一致性审查状态：仅 done 且有结果时返回，供刷新/重连后回显。 */
function restoreConsistency(report: ContractReport): ConsistencyState | null {
  if (report.contract.status !== "done") return null;
  const opinions = report.consistency_opinions ?? [];
  const riskAssessment = report.consistency_risk_assessment ?? null;
  if (opinions.length === 0 && !riskAssessment) return null;
  return {
    status: "done",
    steps: [], // 一致性推理过程不落库，刷新后只回显结果。
    hasOpinion: opinions.length > 0,
    opinions,
    riskAssessment,
  };
}

function mergeClauseReviews(
  current: Record<string, ClauseReview>,
  restored: Record<string, ClauseReview>,
): Record<string, ClauseReview> {
  const merged: Record<string, ClauseReview> = { ...restored };
  for (const [clauseId, live] of Object.entries(current)) {
    const fromReport = merged[clauseId];
    if (!fromReport) {
      merged[clauseId] = live;
      continue;
    }
    const keepLiveSteps =
      live.status === "reviewing" ||
      live.steps.length > fromReport.steps.length ||
      fromReport.steps.length === 0;
    merged[clauseId] = {
      ...fromReport,
      status:
        fromReport.status === "done"
          ? "done"
          : live.status === "reviewing"
            ? "reviewing"
            : fromReport.status,
      steps: keepLiveSteps ? live.steps : fromReport.steps,
      opinions: fromReport.opinions.length ? fromReport.opinions : live.opinions,
      hasOpinion: fromReport.hasOpinion || live.hasOpinion,
      riskAssessment: fromReport.riskAssessment ?? live.riskAssessment,
    };
  }
  return merged;
}

function appendThinking(
  steps: ReasoningStep[],
  delta: string,
  agent: string = "review_agent",
): ReasoningStep[] {
  const next = [...steps];
  const last = next[next.length - 1];
  if (last && last.kind === "thinking") {
    next[next.length - 1] = { kind: "thinking", text: last.text + delta, agent };
  } else {
    next.push({ kind: "thinking", text: delta, agent });
  }
  return next;
}

function pushToolStart(steps: ReasoningStep[], data: Record<string, unknown>): ReasoningStep[] {
  const call: ToolCall = {
    call_id: String(data.call_id ?? ""),
    name: String(data.name ?? "tool"),
    args: (data.args as Record<string, unknown>) ?? {},
    status: "running",
    agent: "review_agent",
    startedAt: Date.now(),
  };
  return [...steps, { kind: "tool", call }];
}

function patchToolEnd(steps: ReasoningStep[], data: Record<string, unknown>): ReasoningStep[] {
  const callId = String(data.call_id ?? "");
  return steps.map((s) => {
    if (s.kind !== "tool" || s.call.call_id !== callId) return s;
    const endedAt = Date.now();
    return {
      kind: "tool",
      call: {
        ...s.call,
        status: "done",
        result_preview: data.result_preview as string | undefined,
        citations: (data.citations as ToolCall["citations"]) ?? s.call.citations,
        endedAt,
        elapsed_ms: endedAt - s.call.startedAt,
      },
    };
  });
}

function finishRunningTools(steps: ReasoningStep[]): ReasoningStep[] {
  const endedAt = Date.now();
  return steps.map((s) => {
    if (s.kind !== "tool" || s.call.status !== "running") return s;
    return {
      kind: "tool",
      call: {
        ...s.call,
        status: "done",
        result_preview: s.call.result_preview ?? "（该工具调用未完成）",
        endedAt,
        elapsed_ms: endedAt - s.call.startedAt,
      },
    };
  });
}

export const useContractReviewStore = create<ContractReviewState>((set, get) => ({
  contractId: null,
  jobId: null,
  status: "idle",
  summary: null,
  report: null,
  reviewRuns: [],
  currentRunId: null,
  currentRunStartedAt: null,
  clauseReviews: {},
  clauseOrder: [],
  stage: null,
  reportReady: false,
  instructionSent: false,
  consistency: null,
  activeClauseId: null,
  activeTab: "clause",
  panelOpen: true,
  error: null,

  upload: async (file: File, sessionId?: string) => {
    set({
      status: "uploading",
      error: null,
      report: null,
      reviewRuns: [],
      currentRunId: null,
      currentRunStartedAt: null,
      summary: null,
      clauseReviews: {},
      clauseOrder: [],
      stage: null,
      reportReady: false,
      instructionSent: false,
      consistency: null,
    });
    const form = new FormData();
    form.append("file", file);
    if (sessionId) form.append("session_id", sessionId);
    const resp = await apiFetch("/contract-review", { method: "POST", body: form });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      let detail = `HTTP ${resp.status}`;
      try {
        const parsed = JSON.parse(text);
        if (parsed.detail) detail = parsed.detail;
      } catch {
        if (text) detail = text;
      }
      set({ status: "failed", error: detail });
      throw new Error(detail);
    }
    const summary: ContractSummary = await resp.json();
    set({
      contractId: summary.id,
      jobId: summary.job_id,
      summary,
      status: "idle",
      stage: null,
      currentRunId: null,
      currentRunStartedAt: null,
      panelOpen: true,
      activeTab: "clause",
    });
    return summary;
  },

  streamReview: async (contractId: number, opts?: { reset?: boolean }) => {
    if (reviewAbort && activeReviewContractId === contractId && !reviewAbort.signal.aborted) {
      return;
    }
    get().stopReview();
    const controller = new AbortController();
    reviewAbort = controller;
    activeReviewContractId = contractId;
    const shouldReset = opts?.reset ?? true;
    const priorRun = shouldReset ? snapshotCurrentRun(get()) : null;
    const runId = shouldReset
      ? newReviewRunId(contractId)
      : get().currentRunId ?? newReviewRunId(contractId);
    const runStartedAt = shouldReset ? Date.now() : get().currentRunStartedAt ?? Date.now();
    set((s) => ({
      status: "reviewing",
      error: null,
      stage: s.contractId === contractId && !shouldReset ? s.stage ?? "parsing" : "parsing",
      contractId,
      reviewRuns: priorRun ? [...s.reviewRuns, priorRun] : s.reviewRuns,
      currentRunId: runId,
      currentRunStartedAt: runStartedAt,
      ...(shouldReset
        ? {
            clauseReviews: {},
            clauseOrder: [],
            report: null,
            reportReady: false,
            consistency: null,
            activeTab: "clause" as const,
          }
        : { activeTab: "clause" as const }),
    }));

    const updateClause = (clauseId: string, patch: (cr: ClauseReview) => ClauseReview) => {
      set((s) => {
        const cur = s.clauseReviews[clauseId];
        if (!cur) return s;
        return { clauseReviews: { ...s.clauseReviews, [clauseId]: patch(cur) } };
      });
    };

    const onEvent = (ev: SSEEvent) => {
      const data = ev.data || {};
      switch (ev.event) {
        case "status": {
          const stage = data.status as ContractJobStatus | undefined;
          if (stage) set({ stage });
          const clauses = data.clauses as
            | Array<{ clause_id: string; clause_no: string; title: string; category?: string }>
            | undefined;
          if (clauses && clauses.length) {
            const map: Record<string, ClauseReview> = {};
            const order: string[] = [];
            const current = get().clauseReviews;
            for (const c of clauses) {
              const existing = current[c.clause_id];
              map[c.clause_id] = existing
                ? { ...existing, category: c.category ?? existing.category }
                : {
                    clause_id: c.clause_id,
                    clause_no: c.clause_no,
                    title: c.title,
                    status: "pending",
                    steps: [],
                    opinions: [],
                    hasOpinion: false,
                    riskAssessment: null,
                    category: c.category,
                  };
              order.push(c.clause_id);
            }
            set((s) => ({
              clauseReviews: map,
              clauseOrder: order,
              // 一致性审查从一开始就占位展示（pending），待条款审查完成后才真正开始；
              // 不覆盖已进入 reviewing/done 的状态（重连补播场景）。
              consistency:
                s.consistency && s.consistency.status !== "pending"
                  ? s.consistency
                  : {
                      status: "pending" as const,
                      steps: [],
                      hasOpinion: false,
                      opinions: [],
                      riskAssessment: null,
                    },
            }));
            // 解析+切分完成：立即拉取已落库的完整条款（带正文）供「条款视图」即时渲染。
            void get().fetchReport(contractId);
          }
          break;
        }
        case "clause_start":
          updateClause(String(data.clause_id), (cr) => ({ ...cr, status: "reviewing" }));
          break;
        case "clause_think_delta":
          updateClause(String(data.clause_id), (cr) => ({
            ...cr,
            steps: appendThinking(cr.steps, String(data.delta ?? "")),
          }));
          break;
        case "clause_tool_start":
          updateClause(String(data.clause_id), (cr) => ({
            ...cr,
            steps: pushToolStart(cr.steps, data),
          }));
          break;
        case "clause_tool_end":
          updateClause(String(data.clause_id), (cr) => ({
            ...cr,
            steps: patchToolEnd(cr.steps, data),
          }));
          break;
        case "clause_done": {
          const rawOpinions = (data.opinions as Array<Record<string, unknown>>) ?? [];
          const opinions: ReviewOpinion[] = rawOpinions.map((r) => ({
            id: Number(r.id ?? 0),
            clause_id_ref: Number(r.clause_id_ref ?? 0),
            opinion_type: String(r.opinion_type ?? ""),
            review_dimension: String(r.review_dimension ?? ""),
            finding: String(r.finding ?? ""),
            recommendation: String(r.recommendation ?? ""),
            confidence: Number(r.confidence ?? 0),
            citations: (r.citations as ReviewOpinion["citations"]) ?? [],
            clause_id: String(r.clause_id ?? ""),
            created_at: "",
          }));
          const rawAssessment = data.risk_assessment as Record<string, unknown> | null | undefined;
          const riskAssessment: ClauseRiskAssessment | null = rawAssessment
            ? {
                id: Number(rawAssessment.id ?? 0),
                clause_id_ref: Number(rawAssessment.clause_id_ref ?? 0),
                clause_id: String(rawAssessment.clause_id ?? data.clause_id ?? ""),
                risk_level: (rawAssessment.risk_level as ClauseRiskAssessment["risk_level"]) ?? "none",
                rationale: String(rawAssessment.rationale ?? ""),
                affected_party: String(rawAssessment.affected_party ?? ""),
                confidence: Number(rawAssessment.confidence ?? 0),
                created_at: "",
              }
            : null;
          const failed = Boolean(data.failed);
          const skipped = Boolean(data.skipped);
          updateClause(String(data.clause_id), (cr) => ({
            ...cr,
            status: failed ? "failed" : skipped ? "skipped" : "done",
            steps: finishRunningTools(cr.steps),
            opinions,
            hasOpinion: Boolean(data.has_opinion),
            riskAssessment,
            failed,
            skipped,
          }));
          break;
        }
        case "report_ready": {
          // 条款级审查全部落库（aggregate 完成）：这里只预取条款意见，报告气泡必须等
          // 全文一致性审查结束后再显示，避免报告缺少一致性结论。
          void get().fetchReport(contractId);
          break;
        }
        case "consistency_start":
          set((s) => ({
            consistency: {
              status: "reviewing",
              message: "正在进行全文一致性审查…",
              steps: s.consistency?.steps ?? [],
              hasOpinion: false,
              opinions: [],
              riskAssessment: null,
            },
          }));
          break;
        case "consistency_delta":
          set((s) => ({
            consistency: s.consistency
              ? { ...s.consistency, message: String(data.message ?? s.consistency.message) }
              : s.consistency,
          }));
          break;
        case "consistency_think_delta":
          set((s) =>
            s.consistency
              ? {
                  consistency: {
                    ...s.consistency,
                    steps: appendThinking(
                      s.consistency.steps,
                      String(data.delta ?? ""),
                      "consistency_agent",
                    ),
                  },
                }
              : s,
          );
          break;
        case "consistency_done": {
          if (data.failed) {
            set((s) => ({
              consistency: {
                status: "failed",
                steps: s.consistency?.steps ?? [],
                hasOpinion: false,
                opinions: s.consistency?.opinions ?? [],
                riskAssessment: s.consistency?.riskAssessment ?? null,
                error: typeof data.error === "string" ? data.error : "一致性审查失败",
              },
            }));
            void get()
              .fetchReport(contractId)
              .then(() => set({ reportReady: true }));
            break;
          }
          const rawOpinions = (data.opinions as Array<Record<string, unknown>>) ?? [];
          const opinions: ConsistencyOpinion[] = rawOpinions.map((r) => ({
            id: Number(r.id ?? 0),
            opinion_type: String(r.opinion_type ?? ""),
            review_dimension: String(r.review_dimension ?? ""),
            finding: String(r.finding ?? ""),
            recommendation: String(r.recommendation ?? ""),
            related_clause_ids: (r.related_clause_ids as string[] | undefined) ?? [],
            evidence_facts: (r.evidence_facts as string[] | undefined) ?? [],
            confidence: Number(r.confidence ?? 0),
            created_at: String(r.created_at ?? ""),
          }));
          const rawRisk = data.risk_assessment as Record<string, unknown> | null | undefined;
          const riskAssessment: ConsistencyRiskAssessment | null = rawRisk
            ? {
                id: Number(rawRisk.id ?? 0),
                risk_level: (rawRisk.risk_level as ConsistencyRiskAssessment["risk_level"]) ?? "none",
                rationale: String(rawRisk.rationale ?? ""),
                affected_party: String(rawRisk.affected_party ?? ""),
                confidence: Number(rawRisk.confidence ?? 0),
                created_at: String(rawRisk.created_at ?? ""),
              }
            : null;
          set((s) => ({
            consistency: {
              status: "done",
              steps: s.consistency?.steps ?? [],
              hasOpinion: Boolean(data.has_opinion),
              opinions,
              riskAssessment,
              note: typeof data.note === "string" ? data.note : undefined,
            },
          }));
          void get()
            .fetchReport(contractId)
            .then(() => set({ reportReady: true }));
          break;
        }
        case "overview_start": {
          // 总览由审查图（assemble_report）实时产出；仅当聊天面板正看着这份合同时注入。
          if (useChatStore.getState().currentContractId === contractId) {
            useChatStore.getState().beginContractOverview();
          }
          break;
        }
        case "overview_think_delta":
          useChatStore.getState().appendContractOverview(String(data.delta ?? ""), "thinking");
          break;
        case "overview_delta":
          useChatStore.getState().appendContractOverview(String(data.delta ?? ""), "answer");
          break;
        case "overview_done":
          useChatStore.getState().finishContractOverview();
          break;
        case "stance_required": {
          const cid = Number(data.contract_id);
          const options = (data.options as string[] | undefined) ?? ["甲方", "乙方", "中立"];
          set({ status: "idle", stage: null, error: null });
          useChatStore.setState({
            pendingStance: {
              contractId: Number.isFinite(cid) && cid > 0 ? cid : contractId,
              options,
              source: "review",
            },
          });
          break;
        }
        case "review_not_started":
          set({
            status: "idle",
            stage: null,
            error: typeof data.message === "string" ? data.message : null,
          });
          break;
        case "done":
          set((s) => ({
            status: "done",
            stage: "done",
            summary: s.summary
              ? {
                  ...s.summary,
                  status: "done",
                  risk_count: Number(data.risk_count ?? s.summary.risk_count),
                }
              : s.summary,
          }));
          void get().fetchReport(contractId);
          // 总览已由审查图在 overview_* 事件里实时产出并落库，无需前端再触发。
          break;
        case "error":
          set({ status: "failed", error: String(data.message ?? "审查失败") });
          break;
        default:
          break;
      }
    };

    try {
      await streamReview(contractId, { signal: controller.signal, onEvent });
    } catch (err) {
      if (controller.signal.aborted) return; // 用户主动中断，不算错误
      // SSE 断开不一定意味着审查失败 —— 审查跑在后端后台任务里，与 SSE 连接解耦。
      // 先拉一次 DB 真实状态：done 就直接同步完成态；reviewing/parsing 就静默重连
      // （后端 review_manager 会补播缓存事件）；只有真 failed 才显示错误。
      try {
        const report = await apiJson<ContractReport>(
          `/contract-review/contracts/${contractId}`,
        );
        const dbStatus = report.contract.status;
        if (dbStatus === "done") {
          const restored = restoreClauseReviews(report);
          set((s) => ({
            report,
            status: "done",
            stage: "done",
            error: null,
            reportReady: true,
            consistency: restoreConsistency(report) ?? s.consistency,
            clauseReviews: restored.clauseReviews,
            clauseOrder:
              restored.clauseOrder.length > 0 ? restored.clauseOrder : s.clauseOrder,
            summary: s.summary
              ? { ...s.summary, status: "done", risk_count: report.contract.risk_count }
              : s.summary,
          }));
          return;
        }
        if (dbStatus === "failed") {
          set({
            status: "failed",
            error: report.contract.error || "审查失败",
          });
          return;
        }
        if (isInProgress(dbStatus)) {
          // 仍在进行：静默重连一次（reset=false 保留已收到的进度）
          setTimeout(() => {
            void get().streamReview(contractId, { reset: false });
          }, 1000);
          return;
        }
        set({
          status: "idle",
          stage: null,
          error: null,
        });
      } catch {
        set({
          status: "failed",
          error: err instanceof Error ? err.message : "审查连接中断",
        });
      }
    } finally {
      if (reviewAbort === controller) reviewAbort = null;
      if (activeReviewContractId === contractId) activeReviewContractId = null;
    }
  },

  setPartyStanceAndStart: async (contractId, stance) => {
    const summary = await apiJson<ContractSummary>(
      `/contract-review/contracts/${contractId}/party-stance`,
      {
        method: "PATCH",
        body: JSON.stringify({ party_stance: stance }),
      },
    );
    set((s) => ({
      summary: s.summary ? { ...s.summary, ...summary } : summary,
      contractId,
      jobId: summary.job_id,
      instructionSent: true,
      error: null,
    }));
    await useChatStore.getState().send(`请按${stance}立场审查这份合同`);
  },

  rerun: () => {
    const { contractId } = get();
    if (contractId != null) {
      const prompt = get().status === "failed" ? "请重新审查这份合同" : "请审查这份合同";
      set({ instructionSent: true });
      void useChatStore.getState().send(prompt);
    }
  },

  stopReview: () => {
    if (reviewAbort) {
      reviewAbort.abort();
      reviewAbort = null;
    }
    activeReviewContractId = null;
  },

  fetchReport: async (contractId: number) => {
    try {
      const report = await apiJson<ContractReport>(
        `/contract-review/contracts/${contractId}`,
      );
      const restored = restoreClauseReviews(report);
      const restoredConsistency = restoreConsistency(report);
      set((s) => ({
        report,
        contractId,
        summary: report.contract,
        reportReady: s.reportReady || report.contract.status === "done",
        // 进行中不覆盖实时一致性状态；done 时用落库结果回显（与实时态一致）。
        consistency: restoredConsistency ?? s.consistency,
        clauseReviews:
          s.contractId === contractId && s.status === "reviewing"
            ? mergeClauseReviews(s.clauseReviews, restored.clauseReviews)
            : restored.clauseReviews,
        clauseOrder: restored.clauseOrder.length ? restored.clauseOrder : s.clauseOrder,
      }));
    } catch {
      set({ error: "加载报告失败" });
    }
  },

  loadContract: async (contractId: number) => {
    const cur = get();
    // 同一合同：已有报告，或正在上传/流式审查中 → 不要打断（避免上传后路由切换误中止审查流）。
    if (
      cur.contractId === contractId &&
      (cur.report || cur.status === "reviewing" || cur.status === "uploading")
    ) {
      return;
    }
    get().stopReview();

    const isSwitching = cur.contractId !== null && cur.contractId !== contractId;
    set({
      contractId,
      error: null,
      panelOpen: true,
      ...(isSwitching
        ? {
            report: null,
            summary: null,
            reviewRuns: [],
            currentRunId: null,
            currentRunStartedAt: null,
            clauseReviews: {},
            clauseOrder: [],
            stage: null,
          }
        : {}),
    });
    try {
      const report = await apiJson<ContractReport>(
        `/contract-review/contracts/${contractId}`,
      );
      const st = report.contract.status;
      const restored = restoreClauseReviews(report);
      const loadedStartedAt =
        toMaybeTimestamp(report.contract.started_at) ??
        toMaybeTimestamp(report.contract.created_at) ??
        Date.now();
      set({
        report,
        summary: report.contract,
        jobId: report.contract.job_id,
        currentRunId: get().currentRunId ?? newReviewRunId(contractId),
        currentRunStartedAt: get().currentRunStartedAt ?? loadedStartedAt,
        status:
          st === "done"
            ? "done"
            : st === "failed"
              ? "failed"
              : st === "reviewing"
                ? "reviewing"
                : "idle",
        error: st === "failed" ? report.contract.error || "审查失败" : null,
        reportReady: st === "done",
        consistency: restoreConsistency(report),
        activeTab: "clause",
        ...restored,
      });
      if (isInProgress(st)) {
        void get().streamReview(contractId, { reset: false });
      }
    } catch {
      set({ status: "failed", error: "合同不存在或无权访问" });
    }
  },

  setActiveClause: (id) => set({ activeClauseId: id }),
  setActiveTab: (tab) => set({ activeTab: tab }),
  setPanelOpen: (open) => set({ panelOpen: open }),
  togglePanel: () => set((s) => ({ panelOpen: !s.panelOpen })),

  reset: () => {
    get().stopReview();
    set({
      contractId: null,
      jobId: null,
      status: "idle",
      summary: null,
      report: null,
      reviewRuns: [],
      currentRunId: null,
      currentRunStartedAt: null,
      clauseReviews: {},
      clauseOrder: [],
      stage: null,
      reportReady: false,
      instructionSent: false,
      consistency: null,
      activeClauseId: null,
      activeTab: "clause",
      panelOpen: true,
      error: null,
    });
  },
}));
