"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { useContractReviewStore } from "@/lib/contract-review-store";
import { cn } from "@/lib/utils";
import type { ClauseRiskAssessment } from "@/lib/types";

const COLOR_MAP: Record<string, string> = {
  brand: "text-[var(--color-brand)]",
  rose: "text-rose-300",
  red: "text-red-400",
  amber: "text-amber-400",
  emerald: "text-emerald-400",
};

function StatCard({
  label,
  value,
  color,
}: {
  label: string;
  value: number | string;
  color: string;
}) {
  return (
    <div
      className="flex flex-1 items-baseline justify-between gap-2 rounded-xl px-3 py-2"
      style={{
        background: "rgba(255,255,255,0.03)",
        border: "1px solid rgba(255,255,255,0.06)",
      }}
    >
      <div className="text-[11px] text-[var(--color-fg-faint)]">{label}</div>
      <div
        className={cn(
          "text-[15px] font-bold tabular-nums",
          COLOR_MAP[color] ?? "text-[var(--color-fg)]",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function mergeAssessments(
  reportItems: ClauseRiskAssessment[],
  liveItems: ClauseRiskAssessment[],
): ClauseRiskAssessment[] {
  const merged: ClauseRiskAssessment[] = [];
  const seen = new Set<number>();
  for (const item of [...reportItems, ...liveItems]) {
    if (item.id) {
      if (seen.has(item.id)) continue;
      seen.add(item.id);
    }
    merged.push(item);
  }
  return merged;
}

/** 顶部紧凑横向统计条；审查开始后即展示，并随实时 clause_done 更新。 */
export function StatsSummaryBar() {
  const status = useContractReviewStore((s) => s.status);
  const summary = useContractReviewStore((s) => s.summary);
  const report = useContractReviewStore((s) => s.report);
  const clauseOrder = useContractReviewStore((s) => s.clauseOrder);
  const clauseReviews = useContractReviewStore((s) => s.clauseReviews);

  if (status === "idle" && !summary && !report && clauseOrder.length === 0) return null;

  const assessments = mergeAssessments(
    report?.clause_risk_assessments ?? [],
    Object.values(clauseReviews)
      .map((cr) => cr.riskAssessment)
      .filter((item): item is ClauseRiskAssessment => item != null),
  );
  const totalClauses =
    report?.clauses.length || clauseOrder.length || summary?.parsed_clauses || 0;
  const critical = assessments.filter((r) => r.risk_level === "critical").length;
  const high = assessments.filter((r) => r.risk_level === "high").length;
  const medium = assessments.filter((r) => r.risk_level === "medium").length;
  const low = assessments.filter((r) => r.risk_level === "low").length;
  const noneCount = assessments.filter((r) => r.risk_level === "none").length;

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="flex shrink-0 items-stretch gap-2 border-b border-[var(--color-border)] px-4 py-2"
      style={{ background: "rgba(255,255,255,0.015)" }}
    >
      <StatCard label="总条款" value={totalClauses} color="brand" />
      <StatCard label="严重" value={critical} color="rose" />
      <StatCard label="高危" value={high} color="red" />
      <StatCard label="中危" value={medium} color="amber" />
      <StatCard label="低危" value={low} color="emerald" />
      <StatCard label="无风险" value={noneCount} color="emerald" />
    </motion.div>
  );
}
