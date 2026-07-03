"use client";

import { X } from "lucide-react";
import { useChatStore } from "@/lib/chat-store";

const LABELS: Record<string, string> = {
  甲方: "我是甲方",
  乙方: "我是乙方",
  中立: "中立审查",
};

/**
 * 委托人立场选择卡片。
 * 后端 ensure_stance 节点在发起整份审查前、立场未知时 interrupt 询问，
 * 前端 chat-store 收到 stance_required 事件后置 pendingStance；本组件据此渲染，
 * 用户选定后 submitStance 以 resume 重入 /chat 恢复被暂停的图；
 * 关闭（右上角 ✕）或「终止审查」则 cancelStance 取消本次审查。
 */
export function StancePrompt() {
  const pending = useChatStore((s) => s.pendingStance);
  const submitStance = useChatStore((s) => s.submitStance);
  const cancelStance = useChatStore((s) => s.cancelStance);
  const streaming = useChatStore((s) => s.streaming);
  if (!pending) return null;

  return (
    <div className="mx-auto my-3 max-w-md rounded-xl border border-[var(--color-border)] bg-white/[0.03] p-4">
      <div className="mb-3 flex items-start gap-2">
        <p className="flex-1 text-sm font-medium text-[var(--color-fg)]">
          请选择你在本合同中的立场，我将站在你的角度审查：
        </p>
        {/* 右上角 ✕：关闭即终止本次审查 */}
        <button
          type="button"
          aria-label="关闭并终止审查"
          onClick={() => void cancelStance()}
          className="-mr-1 -mt-1 shrink-0 rounded-md p-1 text-[var(--color-fg-faint)] transition-colors hover:bg-white/[0.06] hover:text-[var(--color-fg)]"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        {pending.options.map((opt) => (
          <button
            key={opt}
            type="button"
            disabled={streaming}
            onClick={() => void submitStance(opt)}
            className="rounded-lg border border-[var(--color-border)] bg-white/[0.04] px-4 py-2 text-sm font-medium text-[var(--color-fg)] transition-colors hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-50"
          >
            {LABELS[opt] ?? opt}
          </button>
        ))}
      </div>
      {/* 终止审查：与 ✕ 同义，给出更显式的退出入口 */}
      <div className="mt-3 text-center">
        <button
          type="button"
          onClick={() => void cancelStance()}
          className="text-[12.5px] text-[var(--color-fg-faint)] underline-offset-2 transition-colors hover:text-[var(--color-fg-muted)] hover:underline"
        >
          终止审查
        </button>
      </div>
    </div>
  );
}
