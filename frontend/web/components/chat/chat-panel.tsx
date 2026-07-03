"use client";

import * as React from "react";
import Link from "next/link";
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowDown,
  ArrowRight,
  ArrowUpRight,
  Scale,
  Sparkles,
  UploadCloud,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { ChatInput } from "./chat-input";
import { ContractRiskReportBubble } from "./contract-risk-report-bubble";
import { ContractAttachmentBubble, ContractReviewBubble } from "./contract-review-bubble";
import { MessageBubble } from "./message-bubble";
import { StancePrompt } from "./stance-prompt";
import { useChatStore } from "@/lib/chat-store";
import {
  useContractReviewStore,
  type ContractReviewRunSnapshot,
} from "@/lib/contract-review-store";
import { apiJson } from "@/lib/api";
import type { ChatMessage, LawItem } from "@/lib/types";
import { cn, parseApiDateTime } from "@/lib/utils";

const SUGGESTIONS: Array<{ title: string; hint: string }> = [
  {
    title: "什么是格式条款无效？",
    hint: "快速了解《民法典》中的相关规定。",
  },
  {
    title: "劳动合同有哪些常见风险？",
    hint: "识别竞业、赔偿与解除条款风险。",
  },
  {
    title: "自动续费条款是否合法？",
    hint: "结合消费者权益保护法进行分析。",
  },
  {
    title: "如何评估合同中的法律风险？",
    hint: "基于法条、案例与风险规则综合分析。",
  },
];

const FAB_HIDE_THRESHOLD = 80;

type TimelineItem =
  | { kind: "message"; key: string; message: ChatMessage; timestamp: number; order: number }
  | { kind: "contract-attachment"; key: string; timestamp: number; order: number }
  | {
      kind: "contract-review";
      key: string;
      timestamp: number;
      order: number;
      run?: ContractReviewRunSnapshot;
    }
  | {
      kind: "contract-report";
      key: string;
      timestamp: number;
      order: number;
      run?: ContractReviewRunSnapshot;
    };

function toTimestamp(value: string | number | Date | null | undefined, fallback: number) {
  if (value == null) return fallback;
  const timestamp = parseApiDateTime(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : fallback;
}

/** 历史审查过程必须包含条款过程；仅剩一致性占位的旧快照不进入聊天时间线。 */
function hasReviewRunProcess(run: ContractReviewRunSnapshot) {
  return run.clauseOrder.length > 0 || (run.report?.clauses.length ?? 0) > 0;
}

export function ChatPanel() {
  const messages = useChatStore((s) => s.messages);
  const streaming = useChatStore((s) => s.streaming);
  const send = useChatStore((s) => s.send);
  const stop = useChatStore((s) => s.stop);
  const loadingMessages = useChatStore((s) => s.loadingMessages);
  const hasContract = useChatStore((s) => s.currentContractId != null);
  const contractStatus = useContractReviewStore((s) => s.status);
  const contractReportReady = useContractReviewStore((s) => s.reportReady);
  const contractSummary = useContractReviewStore((s) => s.summary);
  const contractReport = useContractReviewStore((s) => s.report);
  const reviewRuns = useContractReviewStore((s) => s.reviewRuns);
  const currentRunId = useContractReviewStore((s) => s.currentRunId);
  const currentRunStartedAt = useContractReviewStore((s) => s.currentRunStartedAt);

  const router = useRouter();
  const uploadContract = useContractReviewStore((s) => s.upload);
  const contractUploading = useContractReviewStore((s) => s.status) === "uploading";

  const handleContractUpload = React.useCallback(
    async (file: File) => {
      const chat = useChatStore.getState();
      // 合同从属会话：默认绑定到当前会话；上传后只挂载，不自动启动审查。
      let targetSession = chat.currentSessionId ?? undefined;
      if (chat.currentSessionId && chat.currentContractId != null) {
        const ok = window.confirm(
          "该会话已审查过合同。是否新建一个会话来审查新合同？",
        );
        if (!ok) return;
        targetSession = undefined; // 让后端新建会话承载本次审查
      }
      try {
        const summary = await uploadContract(file, targetSession);
        const sid = summary.session_id;
        if (sid) {
          useChatStore.setState({
            currentSessionId: sid,
            currentContractId: summary.id,
          });
          void chat.loadSessions();
          router.prefetch(`/c/${sid}`);
          router.push(`/c/${sid}`);
        }
      } catch (err) {
        toast.error(err instanceof Error ? err.message : "合同上传失败");
      }
    },
    [uploadContract, router],
  );

  const scrollerRef = React.useRef<HTMLDivElement>(null);
  const autoScrollRef = React.useRef(true);
  const [showScrollBtn, setShowScrollBtn] = React.useState(false);

  React.useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;

    const pauseAutoScroll = () => {
      if (autoScrollRef.current) {
        autoScrollRef.current = false;
        setShowScrollBtn(true);
      }
    };
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) pauseAutoScroll();
    };
    const onTouchStart = () => pauseAutoScroll();
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "ArrowUp" || e.key === "PageUp" || e.key === "Home") {
        pauseAutoScroll();
      }
    };
    const onScroll = () => {
      const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      if (dist <= 5) {
        autoScrollRef.current = true;
        setShowScrollBtn(false);
      } else if (!autoScrollRef.current) {
        setShowScrollBtn(dist > FAB_HIDE_THRESHOLD);
      }
    };

    el.addEventListener("wheel", onWheel, { passive: true });
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("keydown", onKeyDown);
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("keydown", onKeyDown);
      el.removeEventListener("scroll", onScroll);
    };
  }, []);

  React.useEffect(() => {
    if (!autoScrollRef.current) return;
    const el = scrollerRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  });

  const prevStreamingRef = React.useRef(false);
  React.useEffect(() => {
    if (streaming && !prevStreamingRef.current) {
      autoScrollRef.current = true;
      setShowScrollBtn(false);
      const el = scrollerRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    }
    prevStreamingRef.current = streaming;
  }, [streaming]);

  const scrollToBottom = React.useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    autoScrollRef.current = true;
    setShowScrollBtn(false);
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, []);

  // 合同会话不显示欢迎页，改用气泡内联审查进度。
  const showWelcome = !loadingMessages && messages.length === 0 && !hasContract;
  const timelineItems = React.useMemo<TimelineItem[]>(() => {
    const latestMessageTimestamp = messages.reduce(
      (latest, message) => Math.max(latest, message.createdAt),
      0,
    );
    const fallbackContractTimestamp = latestMessageTimestamp || Date.now();
    const createdTs = contractSummary?.created_at
      ? toTimestamp(contractSummary.created_at, 0)
      : 0;
    const startedTs = contractSummary?.started_at
      ? toTimestamp(contractSummary.started_at, 0)
      : 0;
    const attachmentTs = createdTs > 0 ? createdTs : fallbackContractTimestamp;
    // 审查过程/报告使用“本次审查开始时刻”作为固定锚点；总览流式消息追加时不会把它挤到下面。
    const reviewTimestamp =
      currentRunStartedAt ?? (startedTs > 0 ? startedTs : attachmentTs + 1);
    const reportTimestamp = reviewTimestamp;

    const items: TimelineItem[] = messages.map((message, index) => ({
      kind: "message",
      key: `message-${message.id}`,
      message,
      timestamp: message.createdAt,
      order: index,
    }));

    for (const run of reviewRuns) {
      if (hasReviewRunProcess(run)) {
        items.push({
          kind: "contract-review",
          key: `contract-review-${run.id}`,
          timestamp: run.timestamp,
          order: messages.length + 0.1,
          run,
        });
      }
      if ((run.status === "done" || run.reportReady) && run.report) {
        items.push({
          kind: "contract-report",
          key: `contract-report-${run.id}`,
          timestamp: run.timestamp,
          order: messages.length + 0.2,
          run,
        });
      }
    }

    if (hasContract) {
      // 附件气泡：合同一旦挂载即常驻（含审查中/完成），不随审查过程消失。
      items.push({
        kind: "contract-attachment",
        key: "contract-attachment",
        timestamp: attachmentTs,
        order: -1,
      });
    }
    if (hasContract && (contractStatus === "reviewing" || contractStatus === "done")) {
      // 审查过程气泡：仅在审查真正开始后出现。
      items.push({
        kind: "contract-review",
        key: `contract-review-current-${currentRunId ?? "active"}`,
        timestamp: reviewTimestamp,
        order: messages.length + 0.1,
      });
    }
    if (hasContract && (contractStatus === "done" || contractReportReady) && contractReport) {
      items.push({
        kind: "contract-report",
        key: `contract-report-current-${currentRunId ?? "active"}`,
        timestamp: reportTimestamp,
        order: messages.length + 0.2,
      });
    }

    return items.sort((a, b) => a.timestamp - b.timestamp || a.order - b.order);
  }, [
    contractReport,
    contractReportReady,
    contractStatus,
    contractSummary,
    currentRunId,
    currentRunStartedAt,
    hasContract,
    messages,
    reviewRuns,
  ]);

  return (
    <div className="relative flex h-full flex-col">
      <div ref={scrollerRef} className="flex-1 overflow-y-auto">
        <div
          className="mx-auto w-full max-w-[900px] px-6 py-8"
          style={{ overflowAnchor: "none" }}
        >
          {showWelcome ? (
            <Welcome onPick={(q) => send(q)} streaming={streaming} />
          ) : (
            <div className="space-y-6">
              <AnimatePresence initial={false}>
                {timelineItems.map((item, idx) => {
                  const isLast = idx === timelineItems.length - 1;
                  if (item.kind === "message") {
                    return (
                      <MessageBubble key={item.key} message={item.message} isLast={isLast} />
                    );
                  }
                  if (item.kind === "contract-attachment") {
                    return <ContractAttachmentBubble key={item.key} />;
                  }
                  if (item.kind === "contract-review") {
                    return <ContractReviewBubble key={item.key} run={item.run} />;
                  }
                  return <ContractRiskReportBubble key={item.key} run={item.run} />;
                })}
              </AnimatePresence>
              <StancePrompt />
            </div>
          )}
        </div>
      </div>

      {/* Scroll-to-bottom FAB */}
      <AnimatePresence>
        {showScrollBtn && (
          <motion.button
            key="scroll-btn"
            initial={{ opacity: 0, scale: 0.8, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.8, y: 8 }}
            transition={{ duration: 0.18 }}
            onClick={scrollToBottom}
            aria-label="回到底部"
            className="absolute right-6 flex h-9 w-9 items-center justify-center rounded-full text-white transition-all hover:scale-105"
            style={{
              bottom: "7.5rem",
              background: "rgba(255, 255, 255, 0.06)",
              border: "1px solid rgba(255, 255, 255, 0.10)",
              backdropFilter: "blur(10px)",
              WebkitBackdropFilter: "blur(10px)",
              boxShadow: "0 8px 24px -8px rgba(0, 0, 0, 0.5)",
            }}
          >
            <ArrowDown className="h-4 w-4 text-[var(--color-fg-muted)]" />
          </motion.button>
        )}
      </AnimatePresence>

      {/* Input — 32px from bottom, max-width matches chat */}
      <div className="px-6 pb-8 pt-2">
        <div className="mx-auto w-full max-w-[900px]">
          <ChatInput
            onSend={send}
            onStop={stop}
            streaming={streaming}
            onContractUpload={handleContractUpload}
            contractUploading={contractUploading}
          />
          <p className="mt-2.5 text-center text-[10.5px] text-[var(--color-fg-faint)]">
            回答可能不准确，请核对引用片段。生成内容由人工智能提供。
          </p>
        </div>
      </div>
    </div>
  );
}

/* ════════════════════════════════════════════════════════════
   Welcome screen
   ════════════════════════════════════════════════════════════ */

function Welcome({
  onPick,
  streaming,
}: {
  onPick: (q: string) => void;
  streaming: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4, ease: [0.2, 0.7, 0.2, 1] }}
      className="flex flex-col gap-9 py-8"
    >
      {/* Hero */}
      <div className="flex flex-col items-center gap-5 text-center">
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 0.5, ease: [0.2, 0.7, 0.2, 1] }}
          className="flex h-14 w-14 items-center justify-center rounded-2xl text-white"
          style={{
            background: "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
            boxShadow:
              "0 16px 48px -12px rgba(124, 140, 255, 0.55), inset 0 1px 0 rgba(255,255,255,0.25)",
          }}
        >
          <Sparkles className="h-6 w-6" />
        </motion.div>

        <div className="space-y-2">
          <h1
            className="text-gradient text-[42px] font-bold leading-[1.1] tracking-tight"
            style={{ letterSpacing: "-0.025em" }}
          >
            向你的法律知识库提问
          </h1>
          <p className="mx-auto max-w-xl text-[15px] leading-relaxed text-[var(--color-fg-muted)]">
          上传法律法规、合同或判例后即可开始问答 —— Agent 会自动检索相关法条、案例与上下文，并生成可引用的专业回答。
          </p>
        </div>
      </div>

      {/* Suggestion grid 2x2 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        {SUGGESTIONS.map((s, i) => (
          <motion.button
            key={s.title}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: 0.05 * i, ease: [0.2, 0.7, 0.2, 1] }}
            onClick={() => !streaming && onPick(s.title)}
            disabled={streaming}
            className={cn(
              "lift group relative overflow-hidden text-left",
              "flex flex-col gap-1.5 rounded-[20px] px-5 py-[18px]",
              "transition-all",
            )}
            style={{
              background: "rgba(255, 255, 255, 0.03)",
              border: "1px solid rgba(255, 255, 255, 0.06)",
              backdropFilter: "blur(10px)",
              WebkitBackdropFilter: "blur(10px)",
            }}
          >
            <div className="flex items-start justify-between gap-3">
              <span className="text-[14px] font-medium leading-snug text-[var(--color-fg)]">
                {s.title}
              </span>
              <ArrowUpRight className="h-4 w-4 shrink-0 text-[var(--color-fg-faint)] transition-colors group-hover:text-[var(--color-brand)]" />
            </div>
            <span className="text-[12.5px] leading-relaxed text-[var(--color-fg-faint)]">
              {s.hint}
            </span>
          </motion.button>
        ))}
      </div>

      {/* Two columns: upload region + recent KB */}
      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        <UploadCard />
        <RecentKnowledge />
      </div>
    </motion.div>
  );
}

function UploadCard() {
  return (
    <Link
      href="/knowledge"
      className="lift group flex items-center gap-4 rounded-[20px] px-5 py-[18px] no-underline"
      style={{
        background: "rgba(255, 255, 255, 0.03)",
        border: "1px solid rgba(255, 255, 255, 0.06)",
        backdropFilter: "blur(10px)",
        WebkitBackdropFilter: "blur(10px)",
      }}
    >
      <div
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl text-white"
        style={{
          background:
            "linear-gradient(135deg, rgba(52,211,153,0.85) 0%, rgba(5,150,105,0.85) 100%)",
          boxShadow: "0 8px 24px -8px rgba(52,211,153,0.5)",
        }}
      >
        <UploadCloud className="h-5 w-5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5 text-[14px] font-medium text-[var(--color-fg)]">
          上传法律文件至知识库
          <ArrowRight className="h-3.5 w-3.5 text-[var(--color-fg-faint)] transition-transform group-hover:translate-x-0.5 group-hover:text-emerald-400" />
        </div>
        <div className="mt-0.5 text-[12.5px] text-[var(--color-fg-faint)]">
          自动按条文结构解析并向量化入库。
        </div>
      </div>
    </Link>
  );
}

function RecentKnowledge() {
  const [laws, setLaws] = React.useState<LawItem[] | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    apiJson<LawItem[]>("/law-ingest/laws")
      .then((list) => {
        if (cancelled) return;
        setLaws(list.slice(0, 4));
      })
      .catch(() => {
        if (!cancelled) setLaws([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div
      className="flex flex-col gap-2 rounded-[20px] px-5 py-[18px]"
      style={{
        background: "rgba(255, 255, 255, 0.03)",
        border: "1px solid rgba(255, 255, 255, 0.06)",
        backdropFilter: "blur(10px)",
        WebkitBackdropFilter: "blur(10px)",
      }}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[13px] font-medium text-[var(--color-fg)]">
          <Scale className="h-3.5 w-3.5 text-emerald-400" />
          已入库法律
        </div>
        <Link
          href="/knowledge"
          className="text-[11.5px] text-[var(--color-fg-faint)] transition-colors hover:text-emerald-400"
        >
          查看全部 →
        </Link>
      </div>

      {laws === null ? (
        <div className="space-y-1.5 pt-1">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="h-7 w-full animate-pulse rounded-md bg-white/[0.04]" />
          ))}
        </div>
      ) : laws.length === 0 ? (
        <div className="pt-1 text-[12.5px] text-[var(--color-fg-faint)]">
          法律知识库为空，先去上传法律文件吧。
        </div>
      ) : (
        <ul className="flex flex-col gap-0.5">
          {laws.map((law) => (
            <li
              key={law.law_name}
              className="flex items-center gap-2 truncate rounded-md px-1.5 py-1 text-[12.5px] text-[var(--color-fg-muted)]"
            >
              <Scale className="h-3.5 w-3.5 shrink-0 text-emerald-500/60" />
              <span className="min-w-0 flex-1 truncate" title={law.law_name}>
                《{law.law_name}》
              </span>
              <span className="shrink-0 text-[11px] text-[var(--color-fg-faint)]">
                {law.chunk_count} 条
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
