"use client";

import { create } from "zustand";
import { apiJson } from "./api";
import { streamChat } from "./sse";
import { useContractReviewStore } from "./contract-review-store";
import type {
  ChatMessage,
  Citation,
  ReasoningStep,
  SessionItem,
  SSEEvent,
  ToolCall,
} from "./types";
import { parseApiDateTime } from "./utils";

interface SessionDetail extends SessionItem {
  messages: Array<{
    id: number;
    session_id: string;
    role: ChatMessage["role"];
    content: string;
    citations?: Citation[] | null;
    tool_calls?: Array<{
      call_id: string;
      name: string;
      args?: Record<string, unknown>;
      result_preview?: string;
      citations?: Citation[] | null;
      rewritten?: string | null;
      elapsed_ms?: number;
      agent?: string | null;
    }> | null;
    thinking_ms?: number | null;
    thinking?: string | null;
    reasoning?: Array<{
      type: "thinking" | "tool";
      text?: string;
      call_id?: string;
      name?: string;
      args?: Record<string, unknown>;
      result_preview?: string;
      citations?: Citation[] | null;
      rewritten?: string | null;
      elapsed_ms?: number | null;
      agent?: string | null;
    }> | null;
    images?: string[] | null;
    created_at: string;
  }>;
}

interface ChatState {
  sessions: SessionItem[];
  currentSessionId: string | null;
  /** 当前会话关联的合同 id（会话内开启了合同审查时非空）。 */
  currentContractId: number | null;
  messages: ChatMessage[];
  loadingSessions: boolean;
  loadingMessages: boolean;
  streaming: boolean;
  abortController: AbortController | null;
  /** 委托人立场 HITL：后端 interrupt 询问立场时暂存，前端据此渲染立场选择卡片。 */
  pendingStance: { contractId: number; options: string[]; source?: "chat" | "review" } | null;

  loadSessions: () => Promise<void>;
  newSession: () => Promise<string>;
  selectSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  send: (
    content: string,
    images?: string[],
    opts?: { resume?: string },
  ) => Promise<void>;
  /** 用户在立场卡片选定后调用：以 resume 重入 /chat，恢复被 interrupt 暂停的图。 */
  submitStance: (stance: string) => Promise<void>;
  /** 用户关闭/终止立场卡片：取消本次审查（chat 源以取消哨兵 resume 解除暂停的图）。 */
  cancelStance: () => Promise<void>;
  /** 审查图（assemble_report）产出的合同总览：在当前会话里以流式 assistant 消息呈现。 */
  beginContractOverview: () => void;
  appendContractOverview: (delta: string, kind: "answer" | "thinking") => void;
  finishContractOverview: () => void;
  stop: () => void;
  /** Reset to "no active session" view (welcome). Aborts any in-flight stream. */
  resetToWelcome: () => void;
  reset: () => void;
}

const newId = () => Math.random().toString(36).slice(2, 10);

// 审查图流式总览：当前正在写入的 assistant 消息 id（由审查 SSE 的 overview_* 事件驱动）。
let overviewMessageId: string | null = null;

const baseAssistant = (id: string): ChatMessage => ({
  id,
  role: "assistant",
  content: "",
  thinking: "",
  toolCalls: [],
  steps: [],
  citations: [],
  status: "streaming",
  createdAt: Date.now(),
});

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  currentSessionId: null,
  currentContractId: null,
  messages: [],
  loadingSessions: false,
  loadingMessages: false,
  streaming: false,
  abortController: null,
  pendingStance: null,

  reset: () => {
    set({
      sessions: [],
      currentSessionId: null,
      currentContractId: null,
      messages: [],
      streaming: false,
      abortController: null,
      pendingStance: null,
    });
  },

  resetToWelcome: () => {
    const state = get();
    if (state.abortController) {
      try {
        state.abortController.abort();
      } catch {
        /* ignore */
      }
    }
    set({
      currentSessionId: null,
      currentContractId: null,
      messages: [],
      streaming: false,
      abortController: null,
    });
  },

  loadSessions: async () => {
    set({ loadingSessions: true });
    try {
      const list = await apiJson<SessionItem[]>("/sessions");
      set({ sessions: list });
    } finally {
      set({ loadingSessions: false });
    }
  },

  newSession: async () => {
    const created = await apiJson<SessionItem>("/sessions", { method: "POST" });
    set((s) => ({
      sessions: [created, ...s.sessions],
      currentSessionId: created.id,
      messages: [],
    }));
    return created.id;
  },

  selectSession: async (id) => {
    const state = get();
    // Fast path: this session is already loaded (or actively streaming) so we
    // can keep the existing in-memory messages and skip the network round-trip.
    // This makes URL-based session switching feel instant on revisits.
    if (state.currentSessionId === id && (state.messages.length > 0 || state.streaming)) {
      return;
    }
    // 重新加载同一会话（如上传后路由跳转）时保留 currentContractId，
    // 避免瞬时 null 触发 resetContract() 把正在进行的 SSE 审查中断。
    const sameSession = state.currentSessionId === id;
    set({
      currentSessionId: id,
      currentContractId: sameSession ? state.currentContractId : null,
      loadingMessages: true,
      messages: [],
    });
    try {
      const detail = await apiJson<SessionDetail>(`/sessions/${id}`);
      // RACE GUARD: during the await above, the user may have already triggered
      // a `send()` call (e.g. typed and hit Enter while history was loading).
      // That `send()` appends user+assistant messages to `messages` and flips
      // `streaming=true`. If we naively overwrite with the fetched history,
      // those in-flight messages disappear and subsequent SSE deltas can no
      // longer find the assistant message by id — the user sees nothing.
      // Skip the overwrite in that case; the new conversation will be the
      // source of truth and `loadSessions()` will persist it server-side.
      const cur = get();
      if (cur.currentSessionId !== id || cur.streaming || cur.messages.length > 0) {
        return;
      }
      const msgs: ChatMessage[] = detail.messages.map((m) => {
        // Restore tool calls persisted in the DB back into ToolCall shape.
        const rawToolCalls = m.tool_calls as
          | Array<{
              call_id: string;
              name: string;
              args?: Record<string, unknown>;
              result_preview?: string;
              citations?: Citation[] | null;
              rewritten?: string | null;
              elapsed_ms?: number;
              agent?: string | null;
            }>
          | null
          | undefined;
        const msgTs = parseApiDateTime(m.created_at).getTime();
        // Reconstruct a ToolCall from a persisted tool blob (shared by the flat
        // tool_calls list and the ordered reasoning timeline).
        const toToolCall = (tc: {
          call_id?: string;
          name?: string;
          args?: Record<string, unknown>;
          result_preview?: string;
          citations?: Citation[] | null;
          rewritten?: string | null;
          elapsed_ms?: number | null;
          agent?: string | null;
        }): ToolCall => ({
          call_id: tc.call_id ?? newId(),
          name: tc.name ?? "tool",
          args: tc.args ?? {},
          status: "done" as const,
          agent: tc.agent ?? undefined,
          result_preview: tc.result_preview,
          citations: tc.citations ?? undefined,
          rewritten: tc.rewritten ?? undefined,
          elapsed_ms: tc.elapsed_ms ?? undefined,
          // Reconstruct precise timestamps from elapsed_ms so the duration badge shows correctly.
          endedAt: msgTs,
          startedAt: tc.elapsed_ms != null ? msgTs - tc.elapsed_ms : msgTs,
        });
        const toolCalls: ToolCall[] | undefined = rawToolCalls?.map(toToolCall);

        // Ordered reasoning timeline: prefer the persisted interleaved order;
        // fall back to "all thinking, then all tools" for legacy messages.
        let steps: ReasoningStep[] | undefined;
        if (m.reasoning && m.reasoning.length) {
          steps = m.reasoning.map((s) =>
            s.type === "tool"
              ? { kind: "tool" as const, call: toToolCall(s) }
              : {
                  kind: "thinking" as const,
                  text: s.text ?? "",
                  agent: s.agent ?? undefined,
                },
          );
        } else {
          const fallback: ReasoningStep[] = [];
          if (m.thinking) {
            fallback.push({ kind: "thinking", text: m.thinking, agent: "supervisor" });
          }
          for (const c of toolCalls ?? []) fallback.push({ kind: "tool", call: c });
          steps = fallback.length ? fallback : undefined;
        }
        return {
          id: String(m.id),
          role: m.role,
          content: m.content,
          thinking: m.thinking ?? undefined,
          thinkingMs: m.thinking_ms ?? undefined,
          citations: (m.citations as Citation[] | null) ?? undefined,
          toolCalls: toolCalls?.length ? toolCalls : undefined,
          steps,
          images: m.images && m.images.length ? m.images : undefined,
          status: "done" as const,
          createdAt: msgTs,
        };
      });
      set({ messages: msgs, currentContractId: detail.contract_id ?? null });
    } catch (err) {
      // Session may have been deleted server-side. Surface it to the caller
      // (e.g. the /c/[id] page) so it can route back to the welcome screen.
      set({ currentSessionId: null, messages: [] });
      throw err;
    } finally {
      set({ loadingMessages: false });
    }
  },

  deleteSession: async (id) => {
    await apiJson(`/sessions/${id}`, { method: "DELETE" });
    set((s) => {
      const sessions = s.sessions.filter((x) => x.id !== id);
      const cleared = s.currentSessionId === id;
      const current = cleared ? null : s.currentSessionId;
      return {
        sessions,
        currentSessionId: current,
        currentContractId: cleared ? null : s.currentContractId,
        messages: current ? s.messages : [],
      };
    });
  },

  renameSession: async (id, title) => {
    const updated = await apiJson<SessionItem>(`/sessions/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    set((s) => ({
      sessions: s.sessions.map((x) => (x.id === id ? updated : x)),
    }));
  },

  send: async (content, images, opts) => {
    const cleanImages = (images || []).filter(Boolean);
    if (!content.trim() && cleanImages.length === 0) return;
    if (get().streaming) return;

    const userMsg: ChatMessage = {
      id: newId(),
      role: "user",
      content,
      images: cleanImages.length ? cleanImages : undefined,
      createdAt: Date.now(),
      status: "done",
    };
    const assistantId = newId();
    const assistantMsg = baseAssistant(assistantId);

    const ac = new AbortController();
    set((s) => ({
      messages: [...s.messages, userMsg, assistantMsg],
      streaming: true,
      abortController: ac,
    }));

    const updateAssistant = (patch: (m: ChatMessage) => ChatMessage) => {
      set((s) => ({
        messages: s.messages.map((m) => (m.id === assistantId ? patch(m) : m)),
      }));
    };

    // --- rAF-based delta batching ----------------------------------------
    // High-frequency delta events (answer_delta / think_delta) are buffered
    // and flushed at most once per animation frame (~16 ms).  This keeps
    // the main thread free to handle wheel events, making scrolling smooth
    // even during fast streaming.
    let contentBuffer = "";
    let thinkingBuffer = "";
    let thinkingAgent = "supervisor";
    let rafId: ReturnType<typeof requestAnimationFrame> | null = null;

    const flushDeltas = () => {
      rafId = null;
      const c = contentBuffer;
      const t = thinkingBuffer;
      const tAgent = thinkingAgent;
      if (!c && !t) return;
      contentBuffer = "";
      thinkingBuffer = "";
      updateAssistant((m) => {
        const next: ChatMessage = { ...m };
        if (t) {
          next.thinking = (m.thinking || "") + t;
          // Append into the trailing thinking step (or open a new one) so the
          // reasoning timeline keeps thinking↔tool order as events arrive.
          const steps = m.steps ? [...m.steps] : [];
          const last = steps[steps.length - 1];
          if (last && last.kind === "thinking" && (last.agent || "supervisor") === tAgent) {
            steps[steps.length - 1] = { kind: "thinking", text: last.text + t, agent: tAgent };
          } else {
            steps.push({ kind: "thinking", text: t, agent: tAgent });
          }
          next.steps = steps;
        }
        if (c) next.content = (m.content || "") + c;
        return next;
      });
    };

    const scheduleFlush = () => {
      if (rafId === null) rafId = requestAnimationFrame(flushDeltas);
    };

    const cancelFlush = () => {
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      flushDeltas(); // flush any remaining buffered text immediately
    };
    // --------------------------------------------------------------------

    const handleEvent = (ev: SSEEvent) => {
      const data = ev.data || {};
      switch (ev.event) {
        case "session": {
          const sid = String(data.session_id || "");
          if (sid) {
            const created = !get().currentSessionId;
            set({ currentSessionId: sid });
            if (created) {
              void get().loadSessions();
            }
          }
          break;
        }
        case "rewrite": {
          // Kept for backward-compat; no longer drives any UI element.
          break;
        }
        case "tool_call_start": {
          // Commit any pending thinking as a step BEFORE the tool so the
          // timeline order is "thinking → tool", not "tool → thinking".
          cancelFlush();
          const call: ToolCall = {
            call_id: String(data.call_id || newId()),
            name: String(data.name || "tool"),
            args: (data.args as Record<string, unknown>) || {},
            status: "running",
            agent: typeof data.agent === "string" ? data.agent : undefined,
            startedAt: Date.now(),
          };
          updateAssistant((m) => ({
            ...m,
            toolCalls: [...(m.toolCalls || []), call],
            steps: [...(m.steps || []), { kind: "tool", call }],
          }));
          break;
        }
        case "tool_call_end": {
          const call_id = String(data.call_id || "");
          const cits = (data.citations as Citation[] | undefined) || undefined;
          const rewrittenQ = typeof data.rewritten === "string" ? data.rewritten : undefined;
          const elapsed_ms = typeof data.elapsed_ms === "number" ? data.elapsed_ms : undefined;
          const agent = typeof data.agent === "string" ? data.agent : undefined;
          const endedAt = Date.now();
          const patchCall = (c: ToolCall): ToolCall =>
            c.call_id === call_id
              ? {
                  ...c,
                  status: "done",
                  agent: agent ?? c.agent,
                  result_preview: data.result_preview as string | undefined,
                  citations: cits,
                  rewritten: rewrittenQ,
                  elapsed_ms,
                  endedAt,
                  startedAt: elapsed_ms != null ? endedAt - elapsed_ms : c.startedAt,
                }
              : c;
          updateAssistant((m) => ({
            ...m,
            toolCalls: (m.toolCalls || []).map(patchCall),
            steps: (m.steps || []).map((s) =>
              s.kind === "tool" && s.call.call_id === call_id
                ? { kind: "tool", call: patchCall(s.call) }
                : s,
            ),
            citations: cits && cits.length ? cits : m.citations,
          }));
          break;
        }
        case "think_delta": {
          const d = typeof data.delta === "string" ? data.delta : "";
          if (!d) break;
          const agent = typeof data.agent === "string" ? data.agent : "supervisor";
          if (thinkingBuffer && thinkingAgent !== agent) {
            cancelFlush();
          }
          thinkingAgent = agent;
          thinkingBuffer += d;
          scheduleFlush();
          break;
        }
        case "answer_delta": {
          const d = typeof data.delta === "string" ? data.delta : "";
          if (!d) break;
          contentBuffer += d;
          scheduleFlush();
          break;
        }
        case "review_started": {
          // supervisor 顶层图的 enqueue_review 节点已起后台审查 task；前端打开/重启审查 SSE
          // 同步左侧面板进度。reset=true 等价于「重新审查」按钮，清掉旧条款 / 风险展示。
          const cid = Number(data.contract_id);
          if (Number.isFinite(cid) && cid > 0) {
            set({ currentContractId: cid });
            void useContractReviewStore.getState().streamReview(cid, { reset: true });
          }
          break;
        }
        case "done": {
          cancelFlush(); // flush buffered text before finalizing
          const cits = (data.citations as Citation[] | undefined) || undefined;
          const thinkingMs = typeof data.thinking_ms === "number" ? data.thinking_ms : undefined;
          updateAssistant((m) => ({
            ...m,
            status: "done",
            citations: cits && cits.length ? cits : m.citations,
            thinkingMs: thinkingMs ?? m.thinkingMs,
          }));
          break;
        }
        case "stance_required": {
          // 后端 ensure_stance 节点 interrupt：暂存待选立场，关闭当前 assistant 气泡（无 done 事件），
          // 由 ChatPanel 渲染立场选择卡片；用户选定后 submitStance 以 resume 重入。
          cancelFlush();
          const cid = Number(data.contract_id);
          const options = (data.options as string[] | undefined) ?? ["甲方", "乙方", "中立"];
          updateAssistant((m) => ({ ...m, status: "done" }));
          set({
            pendingStance: { contractId: Number.isFinite(cid) ? cid : 0, options, source: "chat" },
          });
          break;
        }
        case "error": {
          cancelFlush(); // flush buffered text before showing error
          const msg = String(data.message || "未知错误");
          updateAssistant((m) => ({
            ...m,
            status: "error",
            content: m.content || `（错误：${msg}）`,
          }));
          break;
        }
      }
    };

    try {
      await streamChat(
        {
          session_id: get().currentSessionId || undefined,
          content,
          images: cleanImages.length ? cleanImages : undefined,
          resume: opts?.resume,
        },
        { signal: ac.signal, onEvent: handleEvent },
      );
    } catch (err) {
      cancelFlush(); // flush any remaining buffered text
      if (ac.signal.aborted) {
        updateAssistant((m) => ({ ...m, status: "done" }));
      } else {
        const msg = err instanceof Error ? err.message : "请求失败";
        updateAssistant((m) => ({
          ...m,
          status: "error",
          content: m.content || `（错误：${msg}）`,
        }));
      }
    } finally {
      cancelFlush(); // safety net: ensure nothing stays buffered
      set({ streaming: false, abortController: null });
      void get().loadSessions();
    }
  },

  submitStance: async (stance) => {
    const pending = get().pendingStance;
    if (!pending) return;
    set({ pendingStance: null });
    if (pending.source === "review") {
      await useContractReviewStore.getState().setPartyStanceAndStart(
        pending.contractId,
        stance,
      );
      return;
    }
    // 以 resume 重入：content 传所选立场文本便于 UI 留痕；后端用 Command(resume) 恢复暂停的图。
    await get().send(stance, [], { resume: stance });
  },

  cancelStance: async () => {
    const pending = get().pendingStance;
    if (!pending) return;
    set({ pendingStance: null });
    // 终止后挂载气泡回到「待发送指令」态。
    useContractReviewStore.setState({ instructionSent: false });
    // review 源：审查本就处于 idle、无暂停的图，关闭弹窗即可。
    if (pending.source === "review") {
      useContractReviewStore.setState({ status: "idle", stage: null });
      return;
    }
    // chat 源：HITL 图暂停中，用取消哨兵 resume 解除暂停——ensure_stance 据此把「取消审查」
    // 作为真实消息注入并回到 supervisor，由 agent 真实处理并回复（content 同步留痕到 UI/PG）。
    await get().send("取消审查", [], { resume: "__cancel_review__" });
  },

  beginContractOverview: () => {
    const id = newId();
    overviewMessageId = id;
    set((s) => ({ messages: [...s.messages, baseAssistant(id)] }));
  },

  appendContractOverview: (delta, kind) => {
    const id = overviewMessageId;
    if (!id || !delta) return;
    set((s) => ({
      messages: s.messages.map((m) => {
        if (m.id !== id) return m;
        if (kind === "thinking") {
          const steps = m.steps ? [...m.steps] : [];
          const last = steps[steps.length - 1];
          if (last && last.kind === "thinking") {
            steps[steps.length - 1] = { kind: "thinking", text: last.text + delta, agent: "supervisor" };
          } else {
            steps.push({ kind: "thinking", text: delta, agent: "supervisor" });
          }
          return { ...m, thinking: (m.thinking || "") + delta, steps };
        }
        return { ...m, content: (m.content || "") + delta };
      }),
    }));
  },

  finishContractOverview: () => {
    const id = overviewMessageId;
    overviewMessageId = null;
    if (!id) return;
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === id ? { ...m, status: "done" as const } : m,
      ),
    }));
  },

  stop: () => {
    const ac = get().abortController;
    if (ac) ac.abort();
    set({ streaming: false, abortController: null });
  },
}));
