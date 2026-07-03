"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ChevronDown, Sparkles } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Markdown } from "./markdown";
import { ToolCallCard } from "./tool-call-card";
import type { ReasoningStep } from "@/lib/types";
import { cn } from "@/lib/utils";

interface ReasoningTimelineProps {
  steps: ReasoningStep[];
  /** True while the model is still thinking (drives the live "正在思考" timer). */
  active?: boolean;
  durationMs?: number;
  /** Default expanded so the ReAct process is visible without a click. */
  initialOpen?: boolean;
}

export function ReasoningTimeline({
  steps,
  active = false,
  durationMs,
  initialOpen = true,
}: ReasoningTimelineProps) {
  const [open, setOpen] = React.useState(initialOpen);
  const [seconds, setSeconds] = React.useState(0);

  React.useEffect(() => {
    if (!active) return;
    const start = Date.now();
    const timer = setInterval(
      () => setSeconds(Math.floor((Date.now() - start) / 1000)),
      250,
    );
    return () => clearInterval(timer);
  }, [active]);

  // 丢弃空文本片段（工具之间偶发的空白 token），保留工具调用和模型正文输出。
  const visible = steps.filter(
    (s) => s.kind === "tool" || s.text.trim().length > 0,
  );
  if (visible.length === 0 && !active) return null;
  const groups = groupByAgent(visible);

  const summary = active
    ? `正在思考 · ${seconds}s`
    : durationMs
      ? `已思考 · ${(durationMs / 1000).toFixed(1)}s`
      : "已思考";

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button
          className={cn(
            "group flex w-full items-center gap-2 rounded-xl px-3.5 py-2 text-left text-xs",
            "text-[var(--color-fg-muted)] transition-all hover:text-[var(--color-fg)]",
          )}
          style={{
            background: "rgba(255, 255, 255, 0.03)",
            border: "1px solid rgba(255, 255, 255, 0.06)",
            backdropFilter: "blur(10px)",
            WebkitBackdropFilter: "blur(10px)",
          }}
          type="button"
        >
          <span className="relative flex h-4 w-4 items-center justify-center">
            <Sparkles
              className={cn(
                "h-3.5 w-3.5 text-[var(--color-brand)]",
                active && "animate-[pulse-soft_1.6s_ease-in-out_infinite]",
              )}
            />
          </span>
          <span className="flex-1 font-medium">{summary}</span>
          <ChevronDown
            className={cn(
              "h-3.5 w-3.5 transition-transform duration-200",
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
              <div className="relative mt-2">
                {/* Continuous vertical line behind the node gutter. */}
                <div
                  aria-hidden
                  className="absolute top-1 bottom-1 w-px"
                  style={{ left: "6.5px", background: "rgba(255, 255, 255, 0.10)" }}
                />
                <div className="space-y-3">
                  {groups.map((group, gi) => (
                    <div key={`${group.agent}-${gi}`} className="space-y-2">
                      <div className="ml-6 text-[11px] font-medium text-[var(--color-fg-muted)]">
                        {agentLabel(group.agent)}
                      </div>
                      {group.steps.map((step, i) => (
                        <div key={i} className="flex gap-3">
                          <TimelineNode kind={step.kind} />
                          <div className="min-w-0 flex-1">
                            {step.kind === "thinking" ? (
                              <ThinkingSegment text={step.text} />
                            ) : (
                              <ToolCallCard call={step.call} />
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            </motion.div>
          </CollapsibleContent>
        )}
      </AnimatePresence>
    </Collapsible>
  );
}

function stepAgent(step: ReasoningStep) {
  return step.kind === "tool" ? step.call.agent || "supervisor" : step.agent || "supervisor";
}

function groupByAgent(steps: ReasoningStep[]) {
  const groups: Array<{ agent: string; steps: ReasoningStep[] }> = [];
  for (const step of steps) {
    const agent = stepAgent(step);
    const last = groups[groups.length - 1];
    if (last && last.agent === agent) {
      last.steps.push(step);
    } else {
      groups.push({ agent, steps: [step] });
    }
  }
  return groups;
}

function agentLabel(agent: string) {
  if (agent === "review_agent") return "审查员";
  if (agent === "consistency_agent") return "一致性审查员";
  return "监管者";
}

function TimelineNode({ kind }: { kind: ReasoningStep["kind"] }) {
  // Node sits on the line; an opaque ring (page bg) visually "cuts" the line.
  const isTool = kind === "tool";
  return (
    <span className="relative z-10 mt-[6px] shrink-0">
      <span
        className="block h-3.5 w-3.5 rounded-full"
        style={{
          background: isTool ? "var(--color-bg)" : "var(--color-brand)",
          border:
            isTool
              ? "2px solid var(--color-brand)"
              : "2px solid var(--color-bg)",
          boxShadow: "0 0 0 2px var(--color-bg)",
        }}
      />
    </span>
  );
}

function ThinkingSegment({ text }: { text: string }) {
  return (
    <div
      className="rounded-xl px-3.5 py-2.5 text-xs leading-relaxed text-[var(--color-fg-muted)]"
      style={{
        background: "rgba(255, 255, 255, 0.02)",
        border: "1px dashed rgba(255, 255, 255, 0.10)",
      }}
    >
      <Markdown content={text} className="reasoning-markdown" />
    </div>
  );
}

