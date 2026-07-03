"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Check, ChevronDown, Loader2, Wrench, AlertTriangle } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Markdown } from "./markdown";
import type { ToolCall } from "@/lib/types";
import { cn, truncate } from "@/lib/utils";

interface ToolCallCardProps {
  call: ToolCall;
}

const TOOL_LABELS: Record<string, string> = {
  retrieve_documents: "检索知识库",
  verify_law_article: "核验法条",
  search_law: "检索法库",
  list_clauses: "读取条款目录",
  get_clause: "读取条款",
  get_opinions: "读取意见",
  get_clause_risk_assessments: "读取风险评估",
  get_consistency_opinions: "读取一致性意见",
  get_consistency_risk_assessment: "读取一致性风险",
  run_full_review: "重跑全量审查",
  review_clauses: "转交审查员",
  draft_amendment: "转交起草员",
};

const RESULT_SUMMARY_CLASS =
  "max-h-56 overflow-y-scroll overscroll-contain rounded-md p-2 pr-3 text-[11px] leading-[1.55] text-[var(--color-fg-muted)]";

const RESULT_SUMMARY_MONO_CLASS = cn(RESULT_SUMMARY_CLASS, "font-mono");

const RESULT_SUMMARY_STYLE: React.CSSProperties = {
  background: "rgba(0, 0, 0, 0.25)",
  border: "1px solid rgba(255, 255, 255, 0.04)",
  scrollbarGutter: "stable",
};

export function ToolCallCard({ call }: ToolCallCardProps) {
  const [open, setOpen] = React.useState(false);
  const label = TOOL_LABELS[call.name] || call.name;
  const running = call.status === "running";
  const errored = call.status === "error";

  const args = React.useMemo(() => {
    try {
      return JSON.stringify(call.args, null, 2);
    } catch {
      return String(call.args);
    }
  }, [call.args]);

  const queryHint =
    typeof call.args?.query === "string"
      ? truncate(call.args.query as string, 80)
      : "";

  const elapsed =
    call.endedAt && call.startedAt
      ? `${((call.endedAt - call.startedAt) / 1000).toFixed(1)}s`
      : null;

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className="group flex w-full items-center gap-2.5 rounded-xl px-3.5 py-2 text-left text-xs transition-colors"
          style={{
            background: running
              ? "rgba(124, 140, 255, 0.06)"
              : errored
                ? "rgba(255, 107, 107, 0.06)"
                : "rgba(255, 255, 255, 0.03)",
            border: running
              ? "1px solid rgba(124, 140, 255, 0.35)"
              : errored
                ? "1px solid rgba(255, 107, 107, 0.35)"
                : "1px solid rgba(255, 255, 255, 0.06)",
            backdropFilter: "blur(10px)",
            WebkitBackdropFilter: "blur(10px)",
          }}
        >
          <span className="relative flex h-4 w-4 shrink-0 items-center justify-center">
            {running ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--color-brand)]" />
            ) : errored ? (
              <AlertTriangle className="h-3.5 w-3.5 text-[var(--color-destructive)]" />
            ) : (
              <Check className="h-3.5 w-3.5 text-[var(--color-brand)]" />
            )}
          </span>
          <span className="flex flex-1 items-center gap-1.5 truncate">
            <Wrench className="h-3.5 w-3.5 text-[var(--color-fg-muted)]" />
            <span className="font-medium text-[var(--color-fg)]">{label}</span>
            {queryHint && (
              <span className="truncate text-[var(--color-fg-muted)]">— {queryHint}</span>
            )}
          </span>
          {elapsed && (
            <span className="font-mono text-[10.5px] text-[var(--color-fg-muted)]">{elapsed}</span>
          )}
          <ChevronDown
            className={cn(
              "h-3.5 w-3.5 text-[var(--color-fg-muted)] transition-transform duration-200",
              open && "rotate-180",
            )}
          />
        </button>
      </CollapsibleTrigger>
      <AnimatePresence initial={false}>
        {open && (
          <CollapsibleContent forceMount asChild>
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.22, ease: [0.2, 0.7, 0.2, 1] }}
              className="overflow-hidden"
            >
              <div
                className="mt-1.5 space-y-2 rounded-xl p-3.5 text-xs"
                style={{
                  background: "rgba(255, 255, 255, 0.02)",
                  border: "1px dashed rgba(255, 255, 255, 0.10)",
                }}
              >
                <div>
                  <div className="mb-1 font-medium text-[var(--color-fg-muted)]">输入参数</div>
                  <pre
                    className="whitespace-pre-wrap break-words rounded-md p-2 font-mono text-[11px]"
                    style={{
                      background: "rgba(0, 0, 0, 0.25)",
                      border: "1px solid rgba(255, 255, 255, 0.04)",
                    }}
                  >
                    {args}
                  </pre>
                </div>
                {call.rewritten && (
                  <div>
                    <div className="mb-1 font-medium text-[var(--color-fg-muted)]">向量检索改写</div>
                    <div
                      className="break-words rounded-md p-2 text-[11px] leading-relaxed text-[var(--color-fg-muted)]"
                      style={{
                        background: "rgba(124, 140, 255, 0.06)",
                        border: "1px solid rgba(124, 140, 255, 0.18)",
                      }}
                    >
                      {call.rewritten}
                    </div>
                  </div>
                )}
                {(call.citations?.length ?? 0) > 0 ? (
                  <div>
                    <div className="mb-1 font-medium text-[var(--color-fg-muted)]">
                      结果摘要
                    </div>
                    <div
                      // Each citation rendered on its own line as
                      // `[N] (source=..., page=..., section=...)` — no
                      // body text, so the box stays compact even when the
                      // tool returned many hits. When the list overflows
                      // the fixed height the box becomes scrollable.
                      className={RESULT_SUMMARY_MONO_CLASS}
                      style={RESULT_SUMMARY_STYLE}
                    >
                      {call.citations!.map((c) => {
                        const parts = [`source=${c.source}`];
                        if (c.page != null) parts.push(`page=${c.page}`);
                        if (c.headings) parts.push(`section=${c.headings}`);
                        return (
                          <div key={`${c.index}-${c.chunk_id || c.source}`} className="break-words">
                            <span className="text-[var(--color-fg)]">[{c.index}]</span>{" "}
                            ({parts.join(", ")})
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ) : call.result_preview ? (
                  <div>
                    <div className="mb-1 font-medium text-[var(--color-fg-muted)]">
                      结果摘要
                    </div>
                    <div
                      className={cn(RESULT_SUMMARY_CLASS, "break-words")}
                      style={RESULT_SUMMARY_STYLE}
                    >
                      <Markdown content={call.result_preview} className="tool-result-markdown" />
                    </div>
                  </div>
                ) : null}
                {call.citations && call.citations.length > 0 && (
                  <div className="text-[var(--color-fg-muted)]">
                    命中 {call.citations.length} 个片段
                  </div>
                )}
              </div>
            </motion.div>
          </CollapsibleContent>
        )}
      </AnimatePresence>
    </Collapsible>
  );
}
