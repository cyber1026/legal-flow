"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { Loader2, Scale, Sparkles } from "lucide-react";
import { Markdown } from "./markdown";
import { ReasoningTimeline } from "./reasoning-timeline";
import { CitationsPanel } from "./citations-panel";
import { useContractReviewStore } from "@/lib/contract-review-store";
import type { ChatMessage, ReasoningStep } from "@/lib/types";

interface MessageBubbleProps {
  message: ChatMessage;
  /** 是否为时间线最后一项；空的审查触发轮气泡仅在末尾且审查进行中时显示「审查中」占位。 */
  isLast?: boolean;
}

function MessageBubbleImpl({ message, isLast = false }: MessageBubbleProps) {
  const isUser = message.role === "user";
  // 审查是否进行中（含上传/解析）——决定空 assistant 气泡显示「审查中」占位还是不渲染。
  const reviewInProgress = useContractReviewStore(
    (s) => s.status === "reviewing" || s.status === "uploading",
  );

  // ── User: brand-coloured pill ────────────────────────────────
  if (isUser) {
    const hasImages = (message.images?.length ?? 0) > 0;
    const hasText = (message.content || "").trim().length > 0;
    return (
      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.22, ease: [0.2, 0.7, 0.2, 1] }}
        className="flex flex-col items-end gap-2"
      >
        {hasImages && (
          <div className="flex max-w-[78%] flex-wrap justify-end gap-2">
            {message.images!.map((url, i) => (
              <UserImageThumb key={i} url={url} />
            ))}
          </div>
        )}
        {(hasText || !hasImages) && (
          <div
            className="user-message-bubble max-w-[78%] px-5 py-3 text-[15px] leading-relaxed text-white [&::selection]:bg-white [&::selection]:text-[#2a3a9e] [&_*::selection]:bg-white [&_*::selection]:text-[#2a3a9e]"
            style={{
              background: "linear-gradient(135deg, #7C8CFF 0%, #6878FF 100%)",
              borderRadius: "20px",
              borderTopRightRadius: "6px",
              boxShadow: "0 8px 28px -10px rgba(124, 140, 255, 0.55)",
            }}
          >
            <Markdown content={message.content || ""} />
          </div>
        )}
      </motion.div>
    );
  }

  // ── Assistant: glass card ────────────────────────────────────
  const streaming = message.status === "streaming";
  const hasAnyAnswer = (message.content || "").trim().length > 0;
  // Reasoning timeline (thinking ↔ tool calls interleaved in arrival order).
  const steps: ReasoningStep[] = message.steps || [];
  const activeToolRunning =
    streaming &&
    steps.some((s) => s.kind === "tool" && s.call.status === "running");
  // Only activate the thinking animation when the model has actually emitted
  // reasoning tokens (think_delta). Non-thinking models (GLM-4.6V, etc.) are
  // simply slow — we must not misread their latency as a "thinking" phase.
  const hasThinkingContent = steps.some(
    (s) => s.kind === "thinking" && s.text.trim().length > 0,
  );
  const thinkingActive = streaming && !hasAnyAnswer && hasThinkingContent;
  const showSkeleton =
    streaming && !hasAnyAnswer && steps.length === 0;

  // 空的、已结束的 assistant 气泡：来自「选完委托人立场 resume」那一轮——后端只起后台审查、
  // 不产出任何文本。审查进行中（且为末尾气泡）显示「审查中」占位，满足用户的进度感知；
  // 否则不渲染孤零零的空头像（审查完成后总览是另一条带内容的气泡，不需要这个空壳）。
  const isEmptyFinalized = !streaming && !hasAnyAnswer && steps.length === 0;
  if (isEmptyFinalized) {
    if (reviewInProgress && isLast) return <ReviewingPlaceholder />;
    return null;
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0.7, 0.2, 1] }}
      className="flex items-start gap-3"
    >
      {/* Brand orb */}
      <div
        className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-white"
        style={{
          background: "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
          boxShadow: "0 4px 18px -4px rgba(124, 140, 255, 0.5)",
        }}
      >
        <Sparkles className="h-3.5 w-3.5" />
      </div>

      <div className="min-w-0 flex-1 space-y-3">
        {steps.length > 0 && (
          <ReasoningTimeline
            steps={steps}
            active={thinkingActive}
            durationMs={message.thinkingMs}
          />
        )}

        {showSkeleton && <SkeletonText />}

        {activeToolRunning && !hasAnyAnswer && (
          <p className="text-sm italic text-[var(--color-fg-muted)]">正在检索知识库…</p>
        )}

        {hasAnyAnswer && (
          <div
            className="min-w-0 max-w-full overflow-hidden px-5 py-3.5 text-[15px] leading-relaxed text-[var(--color-fg)]"
            style={{
              background: "rgba(255, 255, 255, 0.04)",
              border: "1px solid rgba(255, 255, 255, 0.06)",
              borderRadius: "20px",
              borderTopLeftRadius: "6px",
              backdropFilter: "blur(10px)",
              WebkitBackdropFilter: "blur(10px)",
            }}
          >
            <Markdown content={message.content} />
            {streaming && <BlinkingCaret />}
          </div>
        )}

        {message.status !== "streaming" && (
          <CitationsPanel citations={message.citations} />
        )}

        {message.status === "error" && (
          <p className="text-sm text-[var(--color-destructive)]">回答中出现错误。</p>
        )}
      </div>
    </motion.div>
  );
}

function ReviewingPlaceholder() {
  // 审查触发轮的占位气泡：展示「正在审查合同」让用户在聊天主线里看到进度（详细进度见左侧面板）。
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.22, ease: [0.2, 0.7, 0.2, 1] }}
      className="flex items-start gap-3"
    >
      <div
        className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-white"
        style={{
          background: "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
          boxShadow: "0 4px 18px -4px rgba(124, 140, 255, 0.5)",
        }}
      >
        <Scale className="h-3.5 w-3.5" />
      </div>
      <div className="flex items-center gap-2 pt-1.5 text-[14px] text-[var(--color-fg-muted)]">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-[var(--color-brand)]" />
        正在审查合同，请稍候…
      </div>
    </motion.div>
  );
}

function SkeletonText() {
  return (
    <div className="space-y-2 pt-1">
      <div className="h-3.5 w-2/3 animate-pulse rounded-full bg-white/[0.04]" />
      <div className="h-3.5 w-1/2 animate-pulse rounded-full bg-white/[0.04]" />
    </div>
  );
}

function UserImageThumb({ url }: { url: string }) {
  const [open, setOpen] = React.useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="block h-32 w-32 overflow-hidden rounded-2xl transition-transform hover:scale-[1.02]"
        style={{
          background: "rgba(255,255,255,0.04)",
          border: "1px solid rgba(255,255,255,0.10)",
          boxShadow: "0 8px 24px -10px rgba(0,0,0,0.45)",
        }}
        aria-label="放大查看"
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={url} alt="" className="h-full w-full object-cover" />
      </button>
      {open && (
        <div
          role="dialog"
          onClick={() => setOpen(false)}
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6 backdrop-blur-sm"
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt=""
            className="max-h-full max-w-full rounded-2xl object-contain shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </>
  );
}

function BlinkingCaret() {
  return (
    <span
      aria-hidden
      className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[2px]"
      style={{
        background: "var(--color-brand)",
        animation: "pulse-soft 1.05s ease-in-out infinite",
      }}
    />
  );
}

export const MessageBubble = React.memo(
  MessageBubbleImpl,
  (prev, next) => prev.message === next.message && prev.isLast === next.isLast,
);
