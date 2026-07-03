"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { FileText, List } from "lucide-react";
import { useContractReviewStore } from "@/lib/contract-review-store";
import { ClauseListPanel } from "./clause-list-panel";
import { OriginalPreview } from "./original-preview";
import { StatsSummaryBar } from "./stats-summary-bar";
import { cn } from "@/lib/utils";

const EASE = [0.2, 0.7, 0.2, 1] as const;

/**
 * 会话内的合同审查面板（左侧，可折叠）。
 * 三个 tab：条款视图 / 原文预览 / 风险报告。结构化、可交互的审查结果都在这里；
 * 对话本身（含总览报告消息）走右侧统一聊天。
 */
export function ContractPanel() {
  const activeTab = useContractReviewStore((s) => s.activeTab);
  const setActiveTab = useContractReviewStore((s) => s.setActiveTab);

  return (
    <motion.div
      initial={{ x: -16, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ duration: 0.3, ease: EASE }}
      className="flex h-full min-h-0 flex-col"
    >
      <StatsSummaryBar />

      {/* Tab bar */}
      <div
        className="flex shrink-0 items-center gap-1 px-3 py-2"
        style={{ borderBottom: "1px solid rgba(255,255,255,0.06)" }}
      >
        <TabButton
          active={activeTab === "clause"}
          onClick={() => setActiveTab("clause")}
          icon={<List className="h-3.5 w-3.5" />}
          label="条款视图"
        />
        <TabButton
          active={activeTab === "preview"}
          onClick={() => setActiveTab("preview")}
          icon={<FileText className="h-3.5 w-3.5" />}
          label="原文预览"
        />
      </div>

      {/* Tab content */}
      <div className="min-h-0 flex-1 overflow-hidden">
        {activeTab === "clause" && <ClauseListPanel />}
        {activeTab === "preview" && <OriginalPreview />}
      </div>
    </motion.div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
  badge,
  pulse,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  badge?: number;
  pulse?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[12.5px] font-medium transition-all",
        active
          ? "bg-[var(--color-brand-soft)] text-[var(--color-brand)]"
          : "text-[var(--color-fg-muted)] hover:bg-white/[0.04] hover:text-[var(--color-fg)]",
      )}
    >
      {icon}
      {label}
      {pulse && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[var(--color-brand)]" />
      )}
      {badge != null && (
        <span className="rounded bg-red-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-red-400">
          {badge}
        </span>
      )}
    </button>
  );
}
