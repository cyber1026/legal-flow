import { apiFetch } from "./api";
import type { SSEEvent, SSEEventName } from "./types";

export interface StreamChatOptions {
  signal?: AbortSignal;
  onEvent: (event: SSEEvent) => void;
}

export interface StreamChatRequest {
  session_id?: string | null;
  content: string;
  /** Optional `data:image/...;base64,...` URLs to send to a vision LLM. */
  images?: string[];
  /** HITL 应答（如所选委托人立场）；非空时后端用 Command(resume=...) 恢复被 interrupt 暂停的图。 */
  resume?: string;
}

/** Parse the SSE wire format ("event: <name>\ndata: <json>\n\n") incrementally. */
function* iterEvents(buffer: string): Generator<SSEEvent, string, void> {
  let cursor = 0;
  while (true) {
    const splitIdx = buffer.indexOf("\n\n", cursor);
    if (splitIdx < 0) break;
    const block = buffer.slice(cursor, splitIdx);
    cursor = splitIdx + 2;
    if (!block.trim()) continue;
    let eventName: SSEEventName | undefined;
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim() as SSEEventName;
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (!eventName) continue;
    let parsed: Record<string, unknown> = {};
    const dataStr = dataLines.join("\n");
    if (dataStr) {
      try {
        parsed = JSON.parse(dataStr) as Record<string, unknown>;
      } catch {
        parsed = { raw: dataStr };
      }
    }
    yield { event: eventName, data: parsed };
  }
  return buffer.slice(cursor);
}

/** Drain a streaming Response body, parsing SSE events and firing onEvent. */
async function consumeSSE(
  resp: Response,
  onEvent: (event: SSEEvent) => void,
): Promise<void> {
  if (!resp.body) throw new Error("response has no body");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const iterator = iterEvents(buffer);
      let next = iterator.next();
      while (!next.done) {
        onEvent(next.value);
        next = iterator.next();
      }
      buffer = next.value;
    }
    buffer += decoder.decode();
    const tail = iterEvents(buffer);
    let next = tail.next();
    while (!next.done) {
      onEvent(next.value);
      next = tail.next();
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      // noop
    }
  }
}

export async function streamChat(
  payload: StreamChatRequest,
  { signal, onEvent }: StreamChatOptions,
): Promise<void> {
  const resp = await apiFetch("/chat", {
    method: "POST",
    body: JSON.stringify(payload),
    signal,
  });
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => "");
    throw new Error(`/chat ${resp.status}: ${text || resp.statusText}`);
  }
  await consumeSSE(resp, onEvent);
}

/** Stream a contract's full review (SSE), surfacing per-clause review-agent reasoning. */
export async function streamReview(
  contractId: number,
  { signal, onEvent }: { signal?: AbortSignal; onEvent: (event: SSEEvent) => void },
): Promise<void> {
  const resp = await apiFetch(`/contract-review/contracts/${contractId}/stream`, {
    method: "GET",
    signal,
  });
  if (!resp.ok || !resp.body) {
    const text = await resp.text().catch(() => "");
    throw new Error(`review stream ${resp.status}: ${text || resp.statusText}`);
  }
  await consumeSSE(resp, onEvent);
}
