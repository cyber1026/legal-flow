"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { SessionWorkspace } from "@/components/chat/session-workspace";
import { useChatStore } from "@/lib/chat-store";
import { useContractReviewStore } from "@/lib/contract-review-store";

export default function ChatSessionPage({
  params,
}: {
  // Next 15+ delivers params as a Promise; unwrap with React.use().
  params: Promise<{ sessionId: string }>;
}) {
  const { sessionId } = React.use(params);
  const router = useRouter();
  const selectSession = useChatStore((s) => s.selectSession);
  const currentContractId = useChatStore((s) => s.currentContractId);

  const loadContract = useContractReviewStore((s) => s.loadContract);
  const resetContract = useContractReviewStore((s) => s.reset);

  React.useEffect(() => {
    let cancelled = false;
    selectSession(sessionId).catch(() => {
      // Session no longer exists (404) — bounce back to the welcome screen.
      if (!cancelled) router.replace("/");
    });
    return () => {
      cancelled = true;
    };
  }, [sessionId, selectSession, router]);

  // Sync the contract panel with the current session's attached contract.
  React.useEffect(() => {
    if (currentContractId != null) {
      void loadContract(currentContractId);
    } else {
      resetContract();
    }
  }, [currentContractId, loadContract, resetContract]);

  return <SessionWorkspace />;
}
