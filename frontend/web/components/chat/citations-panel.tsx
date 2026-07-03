"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { BookText, ChevronDown, FileText } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type { Citation } from "@/lib/types";
import { cn } from "@/lib/utils";

interface CitationsPanelProps {
  citations?: Citation[];
}

export function CitationsPanel({ citations }: CitationsPanelProps) {
  const [open, setOpen] = React.useState(false);
  if (!citations || citations.length === 0) return null;

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button
          type="button"
          className="flex items-center gap-2 rounded-full px-3 py-1 text-xs text-[var(--color-fg-muted)] transition-colors hover:text-[var(--color-fg)]"
          style={{
            background: "rgba(124, 140, 255, 0.08)",
            border: "1px solid rgba(124, 140, 255, 0.20)",
          }}
        >
          <BookText className="h-3.5 w-3.5 text-[var(--color-brand)]" />
          <span>引用了 {citations.length} 个片段</span>
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
              <div className="mt-2 space-y-2">
                {citations.map((c) => (
                  <div
                    key={`${c.index}-${c.chunk_id || c.source}`}
                    id={`cite-${c.index}`}
                    className="rounded-xl p-3.5 text-xs"
                    style={{
                      background: "rgba(255, 255, 255, 0.03)",
                      border: "1px solid rgba(255, 255, 255, 0.06)",
                      backdropFilter: "blur(10px)",
                      WebkitBackdropFilter: "blur(10px)",
                    }}
                  >
                    <div className="mb-1.5 flex items-center gap-2 text-[var(--color-fg-muted)]">
                      <span
                        className="inline-flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-semibold text-white"
                        style={{
                          background:
                            "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
                        }}
                      >
                        {c.index}
                      </span>
                      <FileText className="h-3.5 w-3.5" />
                      <span className="font-medium text-[var(--color-fg)]">{c.source}</span>
                      {c.page != null && <span>· p.{c.page}</span>}
                      {c.headings && (
                        <span className="truncate" title={c.headings}>
                          · §{c.headings}
                        </span>
                      )}
                    </div>
                    <p className="whitespace-pre-wrap break-words leading-relaxed text-[var(--color-fg-muted)]">
                      {c.content}
                    </p>
                  </div>
                ))}
              </div>
            </motion.div>
          </CollapsibleContent>
        )}
      </AnimatePresence>
    </Collapsible>
  );
}
