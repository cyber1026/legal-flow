"use client";

import * as React from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import {
  FileText,
  Library,
  LogOut,
  MessageSquarePlus,
  MoreHorizontal,
  Pencil,
  Sparkles,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useChatStore } from "@/lib/chat-store";
import { useAuth } from "@/components/auth-provider";
import { cn, formatDateTime, truncate } from "@/lib/utils";

export function Sidebar() {
  const pathname = usePathname();
  const router = useRouter();
  const { user, logout } = useAuth();

  const sessions = useChatStore((s) => s.sessions);
  const loading = useChatStore((s) => s.loadingSessions);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const renameSession = useChatStore((s) => s.renameSession);

  const [renameTarget, setRenameTarget] = React.useState<{ id: string; title: string } | null>(null);
  const [pendingDeleteIds, setPendingDeleteIds] = React.useState<string[]>([]);
  const pendingDeleteRef = React.useRef(new Set<string>());

  React.useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  // The active session is derived from the URL — no awaiting fetches before
  // routing. This is what gives sidebar clicks a "ChatGPT-like" instant feel.
  const activeSessionId = React.useMemo(() => {
    const m = pathname.match(/^\/c\/([^/]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }, [pathname]);

  const visibleSessions = React.useMemo(
    () => sessions.filter((s) => !pendingDeleteIds.includes(s.id)),
    [sessions, pendingDeleteIds],
  );

  function handleNew() {
    // First message will lazily create the backend session; just route home.
    router.push("/");
  }

  async function handleDelete(id: string) {
    if (pendingDeleteRef.current.has(id)) return;
    pendingDeleteRef.current.add(id);
    setPendingDeleteIds((cur) => [...cur, id]);
    const toastId = `delete-session-${id}`;
    try {
      await deleteSession(id);
      toast.success("会话已删除", { id: toastId });
      // If the deleted session is currently open, fall back to welcome.
      if (activeSessionId === id) router.replace("/");
    } catch {
      pendingDeleteRef.current.delete(id);
      setPendingDeleteIds((cur) => cur.filter((x) => x !== id));
      toast.error("删除失败", { id: toastId });
      return;
    }
    pendingDeleteRef.current.delete(id);
    setPendingDeleteIds((cur) => cur.filter((x) => x !== id));
  }

  async function handleRenameSubmit() {
    if (!renameTarget) return;
    const title = renameTarget.title.trim();
    if (!title) return;
    try {
      await renameSession(renameTarget.id, title);
      setRenameTarget(null);
    } catch {
      toast.error("重命名失败");
    }
  }

  return (
    <aside
      className="flex h-full w-60 shrink-0 flex-col"
      style={{
        background: "var(--color-sidebar)",
        borderRight: "1px solid var(--color-border)",
      }}
    >
      {/* Brand */}
      <div className="flex items-center gap-2.5 px-4 py-4">
        <Link href="/" className="flex items-center gap-2.5 font-medium tracking-tight">
          <span
            className="flex h-7 w-7 items-center justify-center rounded-lg text-white"
            style={{
              background: "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
              boxShadow: "0 4px 18px -4px rgba(124,140,255,0.5)",
            }}
          >
            <Sparkles className="h-3.5 w-3.5" />
          </span>
          <span className="text-[15px] font-semibold tracking-tight text-[var(--color-fg)]">
            Legal Flow
          </span>
        </Link>
      </div>

      {/* New chat */}
      <div className="px-3 pb-2">
        <button
          onClick={handleNew}
          className="lift flex w-full items-center justify-center gap-2 rounded-xl border border-[var(--color-border)] bg-white/[0.03] px-3 py-2 text-sm font-medium text-[var(--color-fg)] transition-colors hover:bg-white/[0.06]"
        >
          <MessageSquarePlus className="h-4 w-4 text-[var(--color-brand)]" />
          新建会话
        </button>
      </div>

      {/* Knowledge link */}
      <nav className="px-3 pb-2">
        <Link
          href="/knowledge"
          className={cn(
            "flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm transition-colors",
            pathname === "/knowledge"
              ? "bg-[var(--color-brand-soft)] text-[var(--color-fg)]"
              : "text-[var(--color-fg-muted)] hover:bg-white/[0.05] hover:text-[var(--color-fg)]",
          )}
        >
          <Library className="h-4 w-4" />
          知识库管理
        </Link>
      </nav>

      <div className="px-3 pt-3 text-[10.5px] font-medium uppercase tracking-[0.12em] text-[var(--color-fg-faint)]">
        会话历史
      </div>
      <div className="flex-1 overflow-y-auto px-2 py-1.5">
        {loading && visibleSessions.length === 0 ? (
          <div className="space-y-1.5 px-1">
            {Array.from({ length: 4 }).map((_, i) => (
              <div
                key={i}
                className="h-8 w-full animate-pulse rounded-lg bg-white/[0.04]"
              />
            ))}
          </div>
        ) : visibleSessions.length === 0 ? (
          <p className="px-2 py-2 text-xs text-[var(--color-fg-faint)]">暂无会话</p>
        ) : (
          <ul className="space-y-0.5">
            <AnimatePresence initial={false}>
              {visibleSessions.map((s) => (
                <motion.li
                  key={s.id}
                  layout
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: 0.18 }}
                >
                  <div
                    className={cn(
                      "group relative flex items-center rounded-lg text-sm transition-colors",
                      activeSessionId === s.id
                        ? "bg-white/[0.07] text-[var(--color-fg)]"
                        : "text-[var(--color-fg-muted)] hover:bg-white/[0.04] hover:text-[var(--color-fg)]",
                    )}
                  >
                    <Link
                      href={`/c/${s.id}`}
                      prefetch={false}
                      className="flex min-w-0 flex-1 items-center gap-2 px-2.5 py-1.5 text-left no-underline"
                    >
                      {s.contract_id != null && (
                        <FileText
                          className="h-3.5 w-3.5 shrink-0 text-emerald-400"
                          aria-label="含合同审查"
                        />
                      )}
                      <span className="min-w-0 flex-1 truncate text-[13px]" title={s.title}>
                        {truncate(s.title, 30)}
                      </span>
                      <span className="ml-auto shrink-0 text-[10px] text-[var(--color-fg-faint)] transition-opacity group-hover:opacity-0">
                        {formatDateTime(s.updated_at)}
                      </span>
                    </Link>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="absolute right-1 top-1/2 h-7 w-7 -translate-y-1/2 opacity-0 group-hover:opacity-100 data-[state=open]:opacity-100"
                          aria-label="会话操作"
                        >
                          <MoreHorizontal className="h-3.5 w-3.5" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onClick={() => setRenameTarget({ id: s.id, title: s.title })}>
                          <Pencil className="h-3.5 w-3.5" />
                          重命名
                        </DropdownMenuItem>
                        <DropdownMenuSeparator />
                        <DropdownMenuItem
                          onClick={() => void handleDelete(s.id)}
                          disabled={pendingDeleteIds.includes(s.id)}
                          className="text-[var(--color-destructive)] focus:text-[var(--color-destructive)]"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {pendingDeleteIds.includes(s.id) ? "删除中…" : "删除"}
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        )}
      </div>

      {/* User chip */}
      <div className="border-t border-[var(--color-border)] px-3 py-3">
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left text-sm transition-colors hover:bg-white/[0.05]">
              <span
                className="flex h-7 w-7 items-center justify-center rounded-full text-[11px] font-semibold text-white"
                style={{
                  background:
                    "linear-gradient(135deg, #7C8CFF 0%, #b482ff 100%)",
                }}
              >
                {(user?.email?.[0] || "?").toUpperCase()}
              </span>
              <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-fg-muted)]">
                {user?.email || "未登录"}
              </span>
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[12rem]">
            <DropdownMenuItem
              onClick={logout}
              className="text-[var(--color-destructive)] focus:text-[var(--color-destructive)]"
            >
              <LogOut className="h-3.5 w-3.5" />
              退出登录
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <Dialog open={!!renameTarget} onOpenChange={(open) => !open && setRenameTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>重命名会话</DialogTitle>
            <DialogDescription>给会话取一个易于识别的名字</DialogDescription>
          </DialogHeader>
          <Input
            value={renameTarget?.title || ""}
            onChange={(e) =>
              setRenameTarget((cur) => (cur ? { ...cur, title: e.target.value } : cur))
            }
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void handleRenameSubmit();
              }
            }}
            placeholder="新名称"
          />
          <DialogFooter className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setRenameTarget(null)}>
              取消
            </Button>
            <Button onClick={handleRenameSubmit}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </aside>
  );
}
