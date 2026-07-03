"use client";

import { cn } from "@/lib/utils";
import type { ClauseRiskAssessment } from "@/lib/types";

const RISK_LEVEL_ORDER = ["critical", "high", "medium", "low", "none"] as const;

const LEVEL_MAP: Record<string, { label: string; className: string }> = {
  safe: {
    label: "无风险",
    className:
      "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  },
  critical: {
    label: "严重",
    className:
      "bg-rose-600/20 text-rose-300 border-rose-500/35",
  },
  high: {
    label: "高危",
    className:
      "bg-red-500/15 text-red-400 border-red-500/25",
  },
  medium: {
    label: "中危",
    className:
      "bg-amber-500/15 text-amber-400 border-amber-500/25",
  },
  low: {
    label: "低危",
    className:
      "bg-emerald-500/15 text-emerald-300 border-emerald-500/25",
  },
  none: {
    label: "无风险",
    className:
      "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  },
};

interface RiskBadgeProps {
  level: string;
  count?: number;
  className?: string;
}

export function RiskBadge({ level, count, className }: RiskBadgeProps) {
  const cfg = LEVEL_MAP[level] ?? {
    label: level,
    className: "bg-emerald-500/15 text-emerald-400 border-emerald-500/25",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold leading-none",
        cfg.className,
        className,
      )}
    >
      {cfg.label}
      {typeof count === "number" ? ` ${count}` : ""}
    </span>
  );
}

export function summarizeRiskLevels(assessments: ClauseRiskAssessment[]) {
  const counts = new Map<string, number>();
  for (const assessment of assessments) {
    counts.set(assessment.risk_level, (counts.get(assessment.risk_level) ?? 0) + 1);
  }
  return RISK_LEVEL_ORDER.filter((level) => counts.has(level)).map((level) => ({
    level,
    count: counts.get(level) ?? 0,
  }));
}

export function RiskBadgeGroup({
  assessments,
  safeWhenEmpty = false,
  className,
}: {
  assessments: ClauseRiskAssessment[];
  safeWhenEmpty?: boolean;
  className?: string;
}) {
  const items = summarizeRiskLevels(assessments);
  if (items.length === 0) {
    if (!safeWhenEmpty) return null;
    return <RiskBadge level="safe" className={className} />;
  }

  return (
    <span className={cn("flex flex-wrap items-center justify-end gap-1", className)}>
      {/* 风险评估为条款级（每条款至多一个评估），不展示冗余的数量 */}
      {items.map((item) => (
        <RiskBadge key={item.level} level={item.level} />
      ))}
    </span>
  );
}

export function riskBorderColor(level: string | undefined): string {
  switch (level) {
    case "critical":
      return "border-l-rose-500/80";
    case "high":
      return "border-l-red-500/70";
    case "medium":
      return "border-l-amber-500/70";
    case "low":
      return "border-l-emerald-500/60";
    default:
      return "border-l-emerald-500/40";
  }
}
