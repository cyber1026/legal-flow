"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  BookOpen,
  Hash,
  Loader2,
  Scale,
  ChevronRight,
} from "lucide-react";
import { toast } from "sonner";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { apiJson } from "@/lib/api";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LawChunkDetail {
  chunk_id: string;
  chunk_index: number;
  article_no: string;
  article_text: string;
  embedding_text: string;
  part: string;
  chapter: string;
  section: string;
  citation_text: string;
  char_count: number;
}

interface LawFileChunks {
  law_name: string;
  total_chunks: number;
  effective_date: string;
  version: string;
  chunks: LawChunkDetail[];
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface LawChunkDetailDialogProps {
  lawName: string | null;
  onOpenChange: (open: boolean) => void;
}

// ---------------------------------------------------------------------------
// Main Dialog
// ---------------------------------------------------------------------------

export function LawChunkDetailDialog({
  lawName,
  onOpenChange,
}: LawChunkDetailDialogProps) {
  const open = lawName !== null;
  const [data, setData] = React.useState<LawFileChunks | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [activeId, setActiveId] = React.useState<string | null>(null);
  const [search, setSearch] = React.useState("");

  React.useEffect(() => {
    if (!lawName) return;
    let cancelled = false;
    setData(null);
    setActiveId(null);
    setSearch("");
    setLoading(true);
    apiJson<LawFileChunks>(
      `/law-ingest/laws/${encodeURIComponent(lawName)}/chunks`,
    )
      .then((d) => {
        if (cancelled) return;
        setData(d);
        if (d.chunks.length) setActiveId(d.chunks[0].chunk_id);
      })
      .catch((err) => {
        if (cancelled) return;
        toast.error(err?.message || "加载法条详情失败");
        onOpenChange(false);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [lawName, onOpenChange]);

  const filtered = React.useMemo(() => {
    if (!data) return [];
    if (!search.trim()) return data.chunks;
    const q = search.trim().toLowerCase();
    return data.chunks.filter(
      (c) =>
        c.article_no.includes(q) ||
        c.article_text.toLowerCase().includes(q) ||
        c.chapter.toLowerCase().includes(q),
    );
  }, [data, search]);

  const active = React.useMemo(
    () => data?.chunks.find((c) => c.chunk_id === activeId) ?? null,
    [data, activeId],
  );

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        aria-describedby={undefined}
        className="flex h-[82vh] w-[min(1100px,94vw)] max-w-none flex-col gap-0 overflow-hidden p-0 sm:rounded-[24px]"
        style={{
          background: "rgba(17, 24, 39, 0.88)",
          border: "1px solid rgba(255, 255, 255, 0.08)",
          backdropFilter: "blur(24px)",
          WebkitBackdropFilter: "blur(24px)",
          boxShadow:
            "0 32px 64px -12px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.06)",
        }}
      >
        <DialogTitle className="sr-only">
          《{lawName}》条文详情
        </DialogTitle>

        {/* ── Header ── */}
        <div className="flex items-start justify-between gap-3 border-b border-white/[0.06] px-6 py-4">
          <div className="min-w-0 flex-1 space-y-1.5">
            <div className="flex items-center gap-2 text-[12px] text-[var(--color-fg-faint)]">
              <Scale className="h-3.5 w-3.5 text-emerald-400" />
              <span>法条 Inspector</span>
            </div>
            <h2 className="truncate text-[17px] font-semibold tracking-tight text-[var(--color-fg)]">
              {lawName ? `《${lawName}》` : "—"}
            </h2>
            {data && (
              <div className="flex flex-wrap items-center gap-2 text-[11.5px] text-[var(--color-fg-muted)]">
                <Pill accent="#34d399">共 {data.total_chunks} 条</Pill>
                {data.effective_date && (
                  <Pill>生效日期：{data.effective_date}</Pill>
                )}
                {data.version && <Pill>版本：{data.version}</Pill>}
              </div>
            )}
          </div>
        </div>

        {/* ── Body ── */}
        {loading || !data ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-3 text-[var(--color-fg-muted)]">
            <Loader2 className="h-5 w-5 animate-spin text-emerald-400" />
            <p className="text-sm">正在加载条文…</p>
          </div>
        ) : (
          <div
            className="grid min-h-0 flex-1"
            style={{ gridTemplateColumns: "280px minmax(0, 1fr)" }}
          >
            {/* Left: article list */}
            <ArticleList
              chunks={data.chunks}
              filtered={filtered}
              activeId={activeId}
              search={search}
              onSearch={setSearch}
              onPick={setActiveId}
            />

            {/* Right: article viewer */}
            <ArticleViewer chunk={active} />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Left panel: article list
// ---------------------------------------------------------------------------

function ArticleList({
  chunks,
  filtered,
  activeId,
  search,
  onSearch,
  onPick,
}: {
  chunks: LawChunkDetail[];
  filtered: LawChunkDetail[];
  activeId: string | null;
  search: string;
  onSearch: (s: string) => void;
  onPick: (id: string) => void;
}) {
  // Group by chapter for context
  let lastChapter = "";

  return (
    <div className="flex min-h-0 flex-col border-r border-white/[0.06]">
      {/* Search bar */}
      <div className="px-3 pt-3 pb-2">
        <input
          type="text"
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder="搜索条文…"
          className="w-full rounded-xl px-3 py-2 text-[12px] outline-none placeholder:text-[var(--color-fg-faint)]"
          style={{
            background: "rgba(255,255,255,0.04)",
            border: "1px solid rgba(255,255,255,0.08)",
            color: "var(--color-fg)",
          }}
        />
      </div>
      <p className="px-4 pb-1 text-[10.5px] text-[var(--color-fg-faint)]">
        {filtered.length} / {chunks.length} 条
      </p>

      {/* Article list */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2 py-1">
        <ul className="flex flex-col gap-0.5">
          {filtered.map((c) => {
            const isActive = c.chunk_id === activeId;
            const showChapterHeader =
              c.chapter && c.chapter !== lastChapter;
            if (c.chapter) lastChapter = c.chapter;

            return (
              <React.Fragment key={c.chunk_id}>
                {showChapterHeader && (
                  <li className="px-3 pb-0.5 pt-2">
                    <p
                      className="truncate text-[10px] font-medium"
                      style={{ color: "rgba(52,211,153,0.7)" }}
                      title={c.chapter}
                    >
                      {c.chapter}
                    </p>
                  </li>
                )}
                <li>
                  <button
                    onClick={() => onPick(c.chunk_id)}
                    className={cn(
                      "group flex w-full items-start gap-2.5 rounded-xl px-3 py-2 text-left transition-colors",
                      isActive
                        ? "bg-emerald-500/10 text-[var(--color-fg)]"
                        : "text-[var(--color-fg-muted)] hover:bg-white/[0.04] hover:text-[var(--color-fg)]",
                    )}
                  >
                    <span
                      className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-[9px] font-bold"
                      style={{
                        background: isActive
                          ? "rgba(52,211,153,0.2)"
                          : "rgba(255,255,255,0.05)",
                        color: isActive
                          ? "#34d399"
                          : "var(--color-fg-faint)",
                      }}
                    >
                      条
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="text-[12.5px] font-medium">
                        {c.article_no}
                      </p>
                      <p className="mt-0.5 line-clamp-2 text-[11px] leading-snug text-[var(--color-fg-faint)]">
                        {c.article_text.slice(0, 60)}
                        {c.article_text.length > 60 ? "…" : ""}
                      </p>
                    </div>
                  </button>
                </li>
              </React.Fragment>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Right panel: article viewer
// ---------------------------------------------------------------------------

function ArticleViewer({ chunk }: { chunk: LawChunkDetail | null }) {
  if (!chunk) {
    return (
      <div className="flex items-center justify-center text-sm text-[var(--color-fg-faint)]">
        从左侧选择一个法条查看详情
      </div>
    );
  }

  return (
    <AnimatePresence mode="wait">
      <motion.div
        key={chunk.chunk_id}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.18, ease: [0.2, 0.7, 0.2, 1] }}
        className="flex min-h-0 flex-col"
      >
        {/* Meta bar */}
        <div className="flex flex-wrap items-center gap-2 px-6 py-3 text-[11.5px] text-[var(--color-fg-muted)]">
          <Pill accent="#34d399">
            <BookOpen className="h-3 w-3" />
            {chunk.article_no}
          </Pill>
          <Pill>
            <Hash className="h-3 w-3" />
            {chunk.chunk_id}
          </Pill>
          <Pill>{chunk.char_count.toLocaleString()} 字符</Pill>
        </div>

        {/* Breadcrumb: 编 > 章 > 节 */}
        {(chunk.part || chunk.chapter || chunk.section) && (
          <div className="flex flex-wrap items-center gap-1 px-6 pb-2 text-[11.5px] text-[var(--color-fg-faint)]">
            {[chunk.part, chunk.chapter, chunk.section]
              .filter(Boolean)
              .map((seg, i, arr) => (
                <React.Fragment key={i}>
                  <span className="break-keep">
                    {seg}
                  </span>
                  {i < arr.length - 1 && (
                    <ChevronRight className="h-3 w-3 shrink-0 opacity-40" />
                  )}
                </React.Fragment>
              ))}
          </div>
        )}

        {/* Article content */}
        <div className="min-h-0 flex-1 overflow-y-auto px-6 pb-6 space-y-4">
          {/* Citation banner */}
          <div
            className="rounded-xl px-4 py-2.5"
            style={{
              background: "rgba(52,211,153,0.06)",
              border: "1px solid rgba(52,211,153,0.2)",
            }}
          >
            <p className="text-[12px] font-medium text-emerald-400">
              {chunk.citation_text}
            </p>
          </div>

          {/* Article text */}
          <div
            className="rounded-2xl px-5 py-4"
            style={{
              background: "rgba(255,255,255,0.03)",
              border: "1px solid rgba(255,255,255,0.06)",
            }}
          >
            <p className="whitespace-pre-wrap break-words text-[13.5px] leading-relaxed text-[var(--color-fg)]">
              {chunk.article_text}
            </p>
          </div>

          {/* Embedding text (collapsible) */}
          <CollapsibleSection title="Embedding Text">
            <p className="whitespace-pre-wrap break-words text-[12.5px] leading-relaxed text-[var(--color-fg-muted)]">
              {chunk.embedding_text}
            </p>
          </CollapsibleSection>
        </div>
      </motion.div>
    </AnimatePresence>
  );
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

function Pill({
  children,
  accent,
}: {
  children: React.ReactNode;
  accent?: string;
}) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full px-2.5 py-0.5"
      style={{
        background: accent ? `${accent}1a` : "rgba(255,255,255,0.05)",
        color: accent ?? "var(--color-fg-muted)",
        border: `1px solid ${accent ? `${accent}40` : "rgba(255,255,255,0.06)"}`,
      }}
    >
      {children}
    </span>
  );
}

function CollapsibleSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = React.useState(false);
  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        className="text-[11.5px] font-medium text-[var(--color-fg-muted)] transition-colors hover:text-[var(--color-fg)]"
      >
        {open ? "▾" : "▸"} {title}
      </button>
      {open && (
        <div
          className="mt-2 rounded-2xl px-5 py-4"
          style={{
            background: "rgba(255,255,255,0.02)",
            border: "1px dashed rgba(255,255,255,0.08)",
          }}
        >
          {children}
        </div>
      )}
    </div>
  );
}
