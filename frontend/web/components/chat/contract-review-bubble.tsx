"use client";

import * as React from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  FileText,
  Loader2,
  MessageSquare,
  Scale,
  ShieldCheck,
} from "lucide-react";
import {
  useContractReviewStore,
  type ConsistencyState,
  type ContractReviewRunSnapshot,
} from "@/lib/contract-review-store";
import { ReasoningTimeline } from "./reasoning-timeline";
import {
  RiskBadge,
  RiskBadgeGroup,
  riskBorderColor,
} from "@/components/contract/risk-badge";
import type { ClauseReview, ContractJobStatus, ReviewOpinion } from "@/lib/types";
import { cn } from "@/lib/utils";

function stageText(stage: ContractJobStatus | null) {
  switch (stage) {
    case "parsing":
      return "正在解析文档…";
    case "embedding":
      return "正在向量化条款…";
    case "reviewing":
      return "正在准备审查…";
    default:
      return "处理中…";
  }
}

/** 复用的品牌头像球（审查体系统一标识）。 */
function BrandOrb() {
  return (
    <div
      className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-white"
      style={{
        background: "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
        boxShadow: "0 4px 18px -4px rgba(124, 140, 255, 0.5)",
      }}
    >
      <Scale className="h-3.5 w-3.5" />
    </div>
  );
}

/** 复用的玻璃卡片外壳。 */
function GlassCard({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="min-w-0 flex-1 overflow-hidden"
      style={{
        background: "rgba(255, 255, 255, 0.04)",
        border: "1px solid rgba(255, 255, 255, 0.06)",
        borderRadius: "20px",
        borderTopLeftRadius: "6px",
        backdropFilter: "blur(10px)",
        WebkitBackdropFilter: "blur(10px)",
      }}
    >
      {children}
    </div>
  );
}

/**
 * 合同附件气泡：持久停在会话上方（按上传时刻落位），与「审查过程」气泡相互独立。
 * 仅展示挂载的合同文件 + 当前审查指令状态（待发送 / 已发送 / 审查中 / 已完成 / 失败）与发起入口；
 * 发起审查后不消失。条款级实时推理与一致性审查的过程在 ContractReviewBubble 里。
 */
export function ContractAttachmentBubble() {
  const status = useContractReviewStore((s) => s.status);
  const summary = useContractReviewStore((s) => s.summary);
  const instructionSent = useContractReviewStore((s) => s.instructionSent);
  const rerun = useContractReviewStore((s) => s.rerun);
  const error = useContractReviewStore((s) => s.error);

  const uploading = status === "uploading";

  // 合同信息加载中（loadContract 在途）：骨架占位。
  if (!summary && status === "idle") {
    return (
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.22 }}
        className="flex items-start gap-3"
      >
        <div className="mt-1 h-7 w-7 shrink-0 animate-pulse rounded-full bg-white/[0.06]" />
        <div className="h-12 flex-1 animate-pulse rounded-[20px] bg-white/[0.04]" />
      </motion.div>
    );
  }
  if (!summary && !uploading) return null;

  // 已发指令但审查尚未真正开始（status 仍 idle）：「指令已发送 · 等待启动」态。
  const instructionPending = status === "idle" && instructionSent;
  const busy = uploading || status === "reviewing" || instructionPending;
  const subline = uploading
    ? "正在上传合同…"
    : status === "failed"
      ? error || "审查失败"
      : status === "done"
        ? "审查已完成，过程与报告见下方。"
        : status === "reviewing"
          ? "审查进行中，实时过程见下方气泡。"
          : instructionSent
            ? "审查指令已发送，正在确认立场并启动审查…"
            : "已挂载到当前会话。点「发送审查指令」或直接说“审查这份合同”即可开始。";
  // 待发起 / 失败 / 已完成时给操作入口；进行中与「已发待启动」不显示按钮（避免重复触发）。
  const showActionBtn =
    (status === "idle" && !instructionSent) || status === "failed" || status === "done";
  const actionLabel =
    status === "failed" ? "发送重审指令" : status === "done" ? "重新审查" : "发送审查指令";

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.2, 0.7, 0.2, 1] }}
      className="flex items-start gap-3"
    >
      <BrandOrb />
      <GlassCard>
        <div className="flex items-center gap-3 px-4 py-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-white/[0.06] text-emerald-300">
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin text-[var(--color-brand)]" />
            ) : status === "done" ? (
              <CheckCircle2 className="h-4 w-4 text-emerald-400" />
            ) : status === "failed" ? (
              <AlertTriangle className="h-4 w-4 text-[var(--color-destructive)]" />
            ) : (
              <FileText className="h-4 w-4" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[13px] font-medium text-[var(--color-fg)]">
              {summary?.filename || summary?.title || "合同文件"}
            </p>
            <p
              className={cn(
                "mt-0.5 truncate text-[12px]",
                status === "failed"
                  ? "text-[var(--color-destructive)]"
                  : "text-[var(--color-fg-faint)]",
              )}
            >
              {subline}
            </p>
          </div>
          {showActionBtn && (
            <button
              onClick={rerun}
              className="ml-1 flex shrink-0 items-center gap-1 rounded-lg px-2.5 py-1 text-[12px] font-medium text-[var(--color-brand)] transition hover:bg-white/[0.05]"
            >
              <MessageSquare className="h-3 w-3" />
              {actionLabel}
            </button>
          )}
          {instructionPending && (
            <span className="ml-1 shrink-0 text-[12px] text-[var(--color-fg-faint)]">
              指令已发送
            </span>
          )}
        </div>
      </GlassCard>
    </motion.div>
  );
}

/**
 * 合同审查过程气泡：展示条款级实时审查（逐条推理）与全文一致性审查。与附件气泡相互独立，
 * 按本次审查开始时刻落位；重新审查时可用冻结快照渲染旧过程。
 */
export function ContractReviewBubble({ run }: { run?: ContractReviewRunSnapshot }) {
  const storeStatus = useContractReviewStore((s) => s.status);
  const storeStage = useContractReviewStore((s) => s.stage);
  const storeClauseOrder = useContractReviewStore((s) => s.clauseOrder);
  const storeClauseReviews = useContractReviewStore((s) => s.clauseReviews);
  const storeSummary = useContractReviewStore((s) => s.summary);
  const storeConsistency = useContractReviewStore((s) => s.consistency);

  const status = run?.status ?? storeStatus;
  const stage = run?.stage ?? storeStage;
  const clauseOrder = run?.clauseOrder ?? storeClauseOrder;
  const clauseReviews = run?.clauseReviews ?? storeClauseReviews;
  const summary = run?.summary ?? storeSummary;
  const consistency = run?.consistency ?? storeConsistency;

  const total = clauseOrder.length;
  const settledCount = clauseOrder.filter((id) => {
    const s = clauseReviews[id]?.status;
    return s === "done" || s === "failed" || s === "skipped";
  }).length;
  const reviewing = isReviewProcessRunning(status, clauseOrder, clauseReviews, consistency);

  // 审查尚未开始（无条款且非进行中/完成）：不渲染过程气泡，附件气泡独立展示挂载状态。
  if (total === 0 && status !== "reviewing" && status !== "done") return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.2, 0.7, 0.2, 1] }}
      className="flex items-start gap-3"
    >
      <BrandOrb />
      <GlassCard>
        {/* Header */}
        <div
          className="flex items-center gap-2 px-4 py-3"
          style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}
        >
          {reviewing ? (
            <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-brand)]" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
          )}
          <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-[var(--color-fg)]">
            {summary?.title || summary?.filename || "合同审查"} · 审查过程
          </span>
          {total > 0 && (
            <span className="shrink-0 text-[12px] text-[var(--color-fg-muted)]">
              {reviewing ? `${settledCount} / ${total}` : `${total} 条款`}
            </span>
          )}
        </div>

        {/* Stage label shown before the clause list appears */}
        {reviewing && total === 0 && (
          <div className="px-4 py-3">
            <p className="text-[12.5px] text-[var(--color-fg-muted)]">{stageText(stage)}</p>
          </div>
        )}

        {/* Clause compact list */}
        {total > 0 && (
          <div className="space-y-0.5 px-3 py-2">
            {clauseOrder.map((id) => {
              const cr = clauseReviews[id];
              if (!cr) return null;
              return <ClauseRow key={id} cr={cr} />;
            })}
          </div>
        )}

        {/* 合同级一致性审查（条款审查后的横向比对，耗时较长 —— 在此显式展示进度/结果） */}
        {consistency && <ConsistencySection state={consistency} />}
      </GlassCard>
    </motion.div>
  );
}

/** 判断“审查过程”是否仍在工作，避免总览生成阶段还显示条款审查转圈。 */
function isReviewProcessRunning(
  status: "idle" | "uploading" | "reviewing" | "done" | "failed",
  clauseOrder: string[],
  clauseReviews: Record<string, ClauseReview>,
  consistency: ConsistencyState | null,
) {
  if (status !== "reviewing") return false;
  if (clauseOrder.length === 0) return true;
  const allClausesSettled = clauseOrder.every((id) => {
    const s = clauseReviews[id]?.status;
    return s === "done" || s === "failed" || s === "skipped";
  });
  if (!allClausesSettled) return true;
  if (!consistency) return true;
  return consistency.status === "pending" || consistency.status === "reviewing";
}

// ─── Consistency review section ─────────────────────────────────────────────

/** 全文一致性审查：进行中显示动画进度，完成后显示风险徽章 + 可展开的跨条款意见。 */
function ConsistencySection({ state }: { state: ConsistencyState }) {
  const [open, setOpen] = React.useState(false);
  const reviewing = state.status === "reviewing";
  const failed = state.status === "failed";
  const pending = state.status === "pending";
  const level = state.riskAssessment?.risk_level;
  const hasSteps = state.steps.length > 0;
  const hasDetail = state.opinions.length > 0 || Boolean(state.riskAssessment?.rationale);
  // 审查中可展开（即便 steps 未到也显示「正在思考」）；或有思考过程；或已完成且有结论。
  // pending 阶段（尚未开始）不可展开。
  const expandable = reviewing || hasSteps || (state.status === "done" && hasDetail);

  // 审查中自动展开，让一致性推理像条款审查一样实时可见；进入终态时收起回干净的徽章摘要。
  // 仅在状态切换时触发，故用户审查中手动收起的操作会被尊重。
  React.useEffect(() => {
    if (reviewing) setOpen(true);
    else if (state.status === "done" || failed) setOpen(false);
  }, [reviewing, failed, state.status]);

  return (
    <div
      className="mx-3 mb-2 rounded-xl border border-white/[0.06] bg-white/[0.015]"
      style={{ borderLeftWidth: "2px" }}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
        onClick={() => expandable && setOpen((v) => !v)}
      >
        {reviewing ? (
          <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-brand)]" />
        ) : failed ? (
          <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-[var(--color-destructive)]" />
        ) : pending ? (
          <Scale className="h-3.5 w-3.5 shrink-0 text-[var(--color-fg-faint)]" />
        ) : (
          <Scale className="h-3.5 w-3.5 shrink-0 text-[var(--color-brand)]" />
        )}
        <span
          className={cn(
            "min-w-0 flex-1 truncate text-[12.5px] font-medium",
            pending ? "text-[var(--color-fg-muted)]" : "text-[var(--color-fg)]",
          )}
        >
          全文一致性审查
        </span>

        {pending && (
          <span className="shrink-0 text-[11.5px] text-[var(--color-fg-faint)]">
            待条款审查后开始
          </span>
        )}
        {reviewing && (
          <span className="shrink-0 text-[11.5px] text-[var(--color-fg-faint)]">
            {state.message || "进行中…"}
          </span>
        )}
        {state.status === "done" &&
          (level && level !== "none" ? (
            <RiskBadge level={level} className="shrink-0" />
          ) : (
            <span className="shrink-0 text-[11.5px] text-emerald-400">未发现跨条款问题</span>
          ))}
        {expandable && (
          <ChevronDown
            className={cn(
              "h-3.5 w-3.5 shrink-0 text-[var(--color-fg-faint)] transition-transform duration-200",
              open && "rotate-180",
            )}
          />
        )}
      </button>

      {failed && state.error && (
        <p className="px-3 pb-2.5 text-[12px] text-[var(--color-destructive)]">{state.error}</p>
      )}

      <AnimatePresence initial={false}>
        {open && expandable && (
          <motion.div
            key="consistency-detail"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="space-y-2 px-3 pb-3">
              {(hasSteps || reviewing) && (
                <ReasoningTimeline steps={state.steps} active={reviewing} initialOpen />
              )}
              {state.riskAssessment?.rationale && (
                <p className="text-[12px] leading-relaxed text-[var(--color-fg-muted)]">
                  {state.riskAssessment.rationale}
                </p>
              )}
              {state.opinions.map((o, i) => (
                <div
                  key={o.id ?? i}
                  className="rounded-lg border border-white/[0.06] bg-white/[0.02] px-3 py-2"
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-[12px] font-medium text-[var(--color-fg)]">
                      {o.opinion_type}
                    </span>
                    <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[11px] text-[var(--color-fg-faint)]">
                      {o.review_dimension}
                    </span>
                    {o.related_clause_ids.length > 0 && (
                      <span className="text-[11px] text-[var(--color-fg-faint)]">
                        关联：{o.related_clause_ids.join("、")}
                      </span>
                    )}
                  </div>
                  <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--color-fg-muted)]">
                    {o.finding}
                  </p>
                  {o.recommendation && (
                    <p className="mt-1 text-[12px] leading-relaxed text-[var(--color-fg-faint)]">
                      建议：{o.recommendation}
                    </p>
                  )}
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ─── Clause row ───────────────────────────────────────────────────────────────

function ClauseRow({ cr }: { cr: ClauseReview }) {
  const [open, setOpen] = React.useState(false);
  const hasSteps = cr.steps.length > 0;
  const hasConclusion =
    cr.status === "done" || cr.status === "failed" || cr.status === "skipped";
  const expandable = hasSteps || hasConclusion;
  const topLevel = cr.riskAssessment?.risk_level;

  return (
    <div
      className={cn("rounded-xl border border-white/[0.04]", riskBorderColor(topLevel))}
      style={{ borderLeftWidth: "2px" }}
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        onClick={() => expandable && setOpen((value) => !value)}
      >
        <ClauseStatusIcon
          status={cr.status}
          hasRisk={cr.riskAssessment?.risk_level !== "none" && cr.riskAssessment != null}
        />
        <span className="shrink-0 text-[11.5px] font-semibold text-[var(--color-fg-muted)]">
          {cr.clause_no || cr.clause_id}
        </span>
        {cr.title && (
          <span className="min-w-0 flex-1 truncate text-[13px] text-[var(--color-fg)]">
            {cr.title}
          </span>
        )}
        <RiskBadgeGroup
          assessments={cr.riskAssessment ? [cr.riskAssessment] : []}
          safeWhenEmpty={cr.status === "done"}
          className="ml-auto"
        />
        {expandable && (
          <ChevronDown
            className={cn(
              "h-3 w-3 shrink-0 text-[var(--color-fg-faint)] transition-transform duration-200",
              open && "rotate-180",
            )}
          />
        )}
      </button>

      <AnimatePresence initial={false}>
        {open && expandable && (
          <motion.div
            key="steps"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18, ease: [0.2, 0.7, 0.2, 1] }}
            className="overflow-hidden"
          >
            <div className="space-y-2 px-3 pb-3">
              {hasSteps && (
                <ReasoningTimeline
                  steps={cr.steps}
                  active={cr.status === "reviewing"}
                  initialOpen
                />
              )}
              {hasConclusion && <ClauseConclusion cr={cr} />}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** 条款审查的结构化结论，区别于上方模型 thinking / 工具轨迹。 */
function ClauseConclusion({ cr }: { cr: ClauseReview }) {
  if (cr.status === "failed") {
    return (
      <p className="text-[12px] text-[var(--color-destructive)]">
        自动审查失败，需人工复核。
      </p>
    );
  }
  if (cr.status === "skipped") {
    return (
      <p className="text-[12px] text-[var(--color-fg-faint)]">
        该条款被识别为样板条款，本轮自动审查已跳过。
      </p>
    );
  }
  const risk = cr.riskAssessment;
  const hasRiskText = Boolean(risk?.rationale?.trim());
  const hasOpinions = cr.opinions.length > 0;
  if (!hasRiskText && !hasOpinions) {
    return <p className="text-[12px] text-[var(--color-fg-faint)]">未发现显著风险。</p>;
  }
  return (
    <div className="space-y-2 rounded-xl border border-white/[0.06] bg-white/[0.02] px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="text-[12px] font-medium text-[var(--color-fg)]">审查结论</span>
        {risk && <RiskBadge level={risk.risk_level} className="shrink-0" />}
      </div>
      {hasRiskText && (
        <p className="text-[12px] leading-relaxed text-[var(--color-fg-muted)]">
          {risk?.rationale}
        </p>
      )}
      {hasOpinions && (
        <div className="space-y-1.5">
          {cr.opinions.map((opinion, index) => (
            <ClauseOpinionLine key={opinion.id ?? index} opinion={opinion} />
          ))}
        </div>
      )}
    </div>
  );
}

/** 条款审查结论中的单条意见摘要。 */
function ClauseOpinionLine({ opinion }: { opinion: ReviewOpinion }) {
  return (
    <div className="rounded-lg border border-white/[0.05] bg-black/[0.08] px-2.5 py-2">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11.5px] font-medium text-[var(--color-fg)]">
          {opinion.opinion_type}
        </span>
        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10.5px] text-[var(--color-fg-faint)]">
          {opinion.review_dimension}
        </span>
      </div>
      <p className="mt-1 text-[12px] leading-relaxed text-[var(--color-fg-muted)]">
        {opinion.finding}
      </p>
      {opinion.recommendation && (
        <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--color-fg-faint)]">
          建议：{opinion.recommendation}
        </p>
      )}
    </div>
  );
}

function ClauseStatusIcon({
  status,
  hasRisk,
}: {
  status: ClauseReview["status"];
  hasRisk: boolean;
}) {
  if (status === "reviewing") {
    return <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-brand)]" />;
  }
  if (status === "done") {
    return hasRisk ? (
      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-400" />
    ) : (
      <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
    );
  }
  return <span className="h-2 w-2 shrink-0 self-center rounded-full bg-white/20" />;
}
