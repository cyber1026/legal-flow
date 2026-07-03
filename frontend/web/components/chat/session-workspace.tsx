"use client";

import { AnimatePresence, motion } from "framer-motion";
import { FileText, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { ChatPanel } from "@/components/chat/chat-panel";
import { ContractPanel } from "@/components/contract/contract-panel";
import { useChatStore } from "@/lib/chat-store";
import { useContractReviewStore } from "@/lib/contract-review-store";
import { truncate } from "@/lib/utils";

const EASE = [0.2, 0.7, 0.2, 1] as const;

export function SessionWorkspace() {
  const currentContractId = useChatStore((s) => s.currentContractId);
  const panelOpen = useContractReviewStore((s) => s.panelOpen);
  const togglePanel = useContractReviewStore((s) => s.togglePanel);
  const summary = useContractReviewStore((s) => s.summary);

  const hasContract = currentContractId != null;
  const contractTitle = summary?.title || summary?.filename || "合同";

  return (
    <motion.div
      layout
      transition={{ duration: 0.34, ease: EASE }}
      className="flex h-full min-h-0 w-full"
    >
      <AnimatePresence initial={false}>
        {hasContract && panelOpen && (
          <motion.aside
            key="contract-panel"
            initial={{ width: 0, opacity: 0, x: -18 }}
            animate={{ width: "44%", opacity: 1, x: 0 }}
            exit={{ width: 0, opacity: 0, x: -18 }}
            transition={{ duration: 0.34, ease: EASE }}
            className="min-h-0 shrink-0 overflow-hidden"
            style={{ borderRight: "1px solid rgba(255,255,255,0.06)" }}
          >
            <ContractPanel />
          </motion.aside>
        )}
      </AnimatePresence>

      <motion.div
        layout
        transition={{ duration: 0.34, ease: EASE }}
        className="flex min-w-0 flex-1 flex-col"
      >
        <AnimatePresence initial={false}>
          {hasContract && (
            <motion.div
              key="contract-toolbar"
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.24, ease: EASE }}
              className="shrink-0"
            >
              <div
                className="flex items-center gap-2 px-4 py-2"
                style={{
                  borderBottom: "1px solid rgba(255,255,255,0.06)",
                  background: "rgba(255,255,255,0.015)",
                }}
              >
                {panelOpen ? (
                  <button
                    onClick={togglePanel}
                    className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12.5px] font-medium text-[var(--color-fg-muted)] transition-colors hover:bg-white/[0.05] hover:text-[var(--color-fg)]"
                    title="收起合同审查面板"
                  >
                    <PanelLeftClose className="h-4 w-4" />
                    收起审查面板
                  </button>
                ) : (
                  <button
                    onClick={togglePanel}
                    className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-[12.5px] font-medium text-[var(--color-fg-muted)] transition-colors hover:bg-white/[0.05] hover:text-[var(--color-fg)]"
                    title="重新打开合同审查面板"
                  >
                    <FileText className="h-4 w-4 text-emerald-400" />
                    <span>已审查《{truncate(contractTitle, 18)}》</span>
                    <span className="flex items-center gap-1 text-[var(--color-brand)]">
                      <PanelLeftOpen className="h-4 w-4" />
                      重新打开
                    </span>
                  </button>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <motion.div
          layout
          transition={{ duration: 0.34, ease: EASE }}
          className="min-h-0 flex-1"
        >
          <ChatPanel />
        </motion.div>
      </motion.div>
    </motion.div>
  );
}
