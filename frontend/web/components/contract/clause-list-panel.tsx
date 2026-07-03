"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { AlertTriangle, ChevronDown, FileText, Loader2 } from "lucide-react";
import { useContractReviewStore } from "@/lib/contract-review-store";
import { RiskBadgeGroup, riskBorderColor, summarizeRiskLevels } from "./risk-badge";
import type { ClauseRiskAssessment, ContractClause } from "@/lib/types";
import { cn } from "@/lib/utils";

const EMPTY_CLAUSES: ContractClause[] = [];
const EMPTY_ASSESSMENTS: ClauseRiskAssessment[] = [];

function clauseAssessments(
  clause: ContractClause,
  assessments: ClauseRiskAssessment[],
): ClauseRiskAssessment[] {
  return assessments.filter((r) => r.clause_id_ref === clause.id);
}

function highestRiskLevel(
  clause: ContractClause,
  assessments: ClauseRiskAssessment[],
): string | undefined {
  return summarizeRiskLevels(clauseAssessments(clause, assessments))[0]?.level;
}

export function ClauseListPanel() {
  const [expandedClauseIds, setExpandedClauseIds] = React.useState<Set<number>>(
    () => new Set(),
  );
  const [expandableClauseIds, setExpandableClauseIds] = React.useState<Set<number>>(
    () => new Set(),
  );
  const textRefs = React.useRef(new Map<number, HTMLParagraphElement>());
  const report = useContractReviewStore((s) => s.report);
  const status = useContractReviewStore((s) => s.status);
  const stage = useContractReviewStore((s) => s.stage);
  const summary = useContractReviewStore((s) => s.summary);
  const error = useContractReviewStore((s) => s.error);
  const activeClauseId = useContractReviewStore((s) => s.activeClauseId);
  const setActiveClause = useContractReviewStore((s) => s.setActiveClause);
  const clauseReviews = useContractReviewStore((s) => s.clauseReviews);
  const clauses = report?.clauses ?? EMPTY_CLAUSES;
  const assessments = report?.clause_risk_assessments ?? EMPTY_ASSESSMENTS;

  React.useLayoutEffect(() => {
    const measure = () => {
      const next = new Set<number>();
      for (const clause of clauses) {
        const el = textRefs.current.get(clause.id);
        if (!el) continue;
        const styles = window.getComputedStyle(el);
        const lineHeight = Number.parseFloat(styles.lineHeight);
        if (!Number.isFinite(lineHeight) || lineHeight <= 0) continue;
        if (el.scrollHeight > lineHeight * 3 + 1) {
          next.add(clause.id);
        }
      }
      setExpandableClauseIds(next);
      setExpandedClauseIds((prev) => {
        const kept = new Set<number>();
        for (const id of prev) {
          if (next.has(id)) kept.add(id);
        }
        return kept;
      });
    };

    measure();
    const resizeObserver = new ResizeObserver(measure);
    for (const el of textRefs.current.values()) {
      resizeObserver.observe(el);
    }
    return () => resizeObserver.disconnect();
  }, [clauses]);

  const toggleClause = React.useCallback(
    (clauseId: number) => {
      setActiveClause(clauseId);
      if (!expandableClauseIds.has(clauseId)) return;
      setExpandedClauseIds((prev) => {
        const next = new Set(prev);
        if (next.has(clauseId)) {
          next.delete(clauseId);
        } else {
          next.add(clauseId);
        }
        return next;
      });
    },
    [expandableClauseIds, setActiveClause],
  );

  // Failed state
  if (status === "failed") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
        <AlertTriangle className="h-9 w-9 text-[var(--color-destructive)]" />
        <div>
          <p className="text-sm font-medium text-[var(--color-destructive)]">
            解析失败
          </p>
          <p className="mt-1 max-w-[280px] break-words text-[12px] leading-relaxed text-[var(--color-fg-muted)]">
            {error || "请稍后重试或联系管理员"}
          </p>
        </div>
      </div>
    );
  }

  // 尚无条款可展示：区分「仅挂载未启动」与「解析/审查进行中」两种状态。
  if (!report || (report.clauses.length === 0 && status !== "done")) {
    // 合同只是挂载到会话、还没有发起审查（status=idle）——此时并未排队，
    // 显示静态占位提示而非加载动画，避免出现误导性的「排队中」。
    const inProgress = status === "uploading" || status === "reviewing";
    if (!inProgress) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-3 px-6 text-center">
          <FileText className="h-8 w-8 text-[var(--color-fg-faint)]" />
          <p className="max-w-[260px] text-sm leading-relaxed text-[var(--color-fg-muted)]">
            合同已挂载到当前会话。在右侧对话告诉我「审查这份合同」即可开始。
          </p>
        </div>
      );
    }
    // 上传/审查进行中：按真实阶段给出文案（不再有「排队中」）。
    const stageKey = status === "uploading" ? "uploading" : stage || "parsing";
    const stageLabel: Record<string, string> = {
      uploading: "正在上传…",
      parsing: "正在解析文档…",
      embedding: "正在向量化条款…",
      reviewing: "正在准备审查…",
    };
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <Loader2 className="h-7 w-7 animate-spin text-[var(--color-brand)]" />
        <p className="text-sm text-[var(--color-fg-muted)]">
          {stageLabel[stageKey] || "处理中…"}
        </p>
      </div>
    );
  }

  // 风险徽标来源：已落库的条款级风险评估 ∪ 审查流实时风险评估。
  // 审查进行中 report 尚空，靠实时评估让徽标随 clause_done 渐进点亮；
  // 二者可能在 done 后并存，按 risk id 去重。
  const liveAssessments: ClauseRiskAssessment[] = Object.values(clauseReviews)
    .map((cr) => cr.riskAssessment)
    .filter((item): item is ClauseRiskAssessment => item != null);
  const mergedAssessments: ClauseRiskAssessment[] = [];
  const seen = new Set<number>();
  for (const r of [...assessments, ...liveAssessments]) {
    if (r.id) {
      if (seen.has(r.id)) continue;
      seen.add(r.id);
    }
    mergedAssessments.push(r);
  }

  return (
    <div className="flex h-full flex-col overflow-y-auto px-3 py-3">
      <div className="space-y-2">
        {clauses.map((clause, i) => {
          const matchedAssessments = clauseAssessments(clause, mergedAssessments);
          const topLevel = highestRiskLevel(clause, mergedAssessments);
          const isActive = activeClauseId === clause.id;
          const expanded = expandedClauseIds.has(clause.id);
          const expandable = expandableClauseIds.has(clause.id);
          const cr = clauseReviews[clause.clause_id];
          const reviewing = cr != null && cr.status === "reviewing";
          const failed = cr?.failed === true;
          const skipped = cr?.skipped === true;

          return (
            <motion.button
              key={clause.id}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.22,
                delay: Math.min(i * 0.03, 0.6),
                ease: [0.2, 0.7, 0.2, 1],
              }}
              onClick={() => toggleClause(clause.id)}
              aria-expanded={expanded}
              className={cn(
                "w-full rounded-xl border border-white/[0.06] px-4 py-3 text-left transition-all",
                riskBorderColor(topLevel),
                isActive
                  ? "bg-white/[0.07] ring-1 ring-[var(--color-brand)]/30"
                  : "bg-white/[0.02] hover:bg-white/[0.05]",
              )}
              style={{ borderLeftWidth: "3px" }}
            >
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-semibold text-[var(--color-fg-muted)]">
                  {clause.clause_no || `#${clause.chunk_index + 1}`}
                </span>
                {clause.title && (
                  <span className="truncate text-[13px] font-medium text-[var(--color-fg)]">
                    {clause.title}
                  </span>
                )}
                {cr?.category && (
                  <span className="shrink-0 rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] text-[var(--color-fg-faint)]">
                    {cr.category}
                  </span>
                )}
                {/* 右侧状态区：整体 ml-auto 右对齐。风险徽章紧邻一个固定宽度的展开槽，
                    无论是否可展开都占位，从而让所有条款的风险徽章右边缘落在同一列。 */}
                <div className="ml-auto flex shrink-0 items-center gap-2">
                  {reviewing && (
                    <span className="flex items-center gap-1 text-[11px] text-[var(--color-fg-faint)]">
                      <Loader2 className="h-3 w-3 animate-spin text-[var(--color-brand)]" />
                      审查中
                    </span>
                  )}
                  {failed && (
                    <span className="text-[11px] text-[var(--color-destructive)]">
                      审查失败 · 需人工复核
                    </span>
                  )}
                  {skipped && (
                    <span className="text-[11px] text-[var(--color-fg-faint)]">
                      已跳过（样板条款）
                    </span>
                  )}
                  <RiskBadgeGroup
                    assessments={matchedAssessments}
                    safeWhenEmpty={!reviewing && !failed && !skipped && status === "done"}
                  />
                  {/* 固定宽度的展开指示槽：可展开时显示旋转箭头，否则留空占位以对齐徽章列 */}
                  <span className="flex w-4 shrink-0 items-center justify-center text-[var(--color-fg-faint)]">
                    {expandable && (
                      <ChevronDown
                        className={cn(
                          "h-3.5 w-3.5 transition-transform",
                          expanded && "rotate-180",
                        )}
                      />
                    )}
                  </span>
                </div>
              </div>

              {clause.section_path && (
                <div className="mt-1 text-[11px] text-[var(--color-fg-faint)]">
                  {clause.section_path}
                </div>
              )}

              <p
                ref={(node) => {
                  if (node) {
                    textRefs.current.set(clause.id, node);
                  } else {
                    textRefs.current.delete(clause.id);
                  }
                }}
                className={cn(
                  "mt-1.5 whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-[var(--color-fg-muted)]",
                  !expanded && "line-clamp-3",
                )}
              >
                {clause.text}
              </p>
            </motion.button>
          );
        })}
      </div>
    </div>
  );
}
