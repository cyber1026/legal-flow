"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { SessionWorkspace } from "@/components/chat/session-workspace";
import { useChatStore } from "@/lib/chat-store";

export default function Home() {
  const router = useRouter();
  const currentId = useChatStore((s) => s.currentSessionId);
  const streaming = useChatStore((s) => s.streaming);

  // On arrival, ensure we render a fresh "new chat" welcome screen. If a
  // previous chat is still streaming we abort it — the user explicitly chose
  // to start over by navigating here.
  React.useEffect(() => {
    useChatStore.getState().resetToWelcome();
    // Run exactly once on mount; do NOT depend on currentId/streaming here,
    // otherwise we'd reset right after the SSE handler assigns the new id.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Once the first user message is sent and the backend assigns a session id,
  // promote the URL so this chat gets a permanent address (`/c/<id>`).
  React.useEffect(() => {
    if (streaming && currentId) {
      router.replace(`/c/${currentId}`);
    }
  }, [streaming, currentId, router]);

  return <SessionWorkspace />;
}
