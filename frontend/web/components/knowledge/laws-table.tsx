"use client";

import * as React from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Scale, Loader2, RefreshCw, Trash2, BookOpen } from "lucide-react";
import { toast } from "sonner";
import { apiFetch, apiJson } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LawChunkDetailDialog } from "./law-chunk-detail-dialog";

interface LawItem {
  law_name: string;
  doc_id: string;
  chunk_count: number;
  effective_date: string;
  version: string;
  law_status: string;
}

interface LawsTableProps {
  refreshKey?: number;
}

export function LawsTable({ refreshKey = 0 }: LawsTableProps) {
  const [laws, setLaws] = React.useState<LawItem[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [deleting, setDeleting] = React.useState<string | null>(null);
  const [activeLaw, setActiveLaw] = React.useState<string | null>(null);

  const reload = React.useCallback(async () => {
    setLoading(true);
    try {
      const list = await apiJson<LawItem[]>("/law-ingest/laws");
      setLaws(list);
    } catch {
      toast.error("加载法律知识库列表失败");
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void reload();
  }, [reload, refreshKey]);

  async function handleDelete(lawName: string) {
    if (
      !confirm(
        `确定从法律知识库删除《${lawName}》吗？\n该法律的所有 chunk 都将从 Milvus 中移除。`,
      )
    )
      return;
    setDeleting(lawName);
    try {
      const resp = await apiFetch(
        `/law-ingest/laws/${encodeURIComponent(lawName)}`,
        { method: "DELETE" },
      );
      if (!resp.ok && resp.status !== 204) {
        const txt = await resp.text();
        throw new Error(txt || `status ${resp.status}`);
      }
      toast.success(`《${lawName}》已从法律知识库删除`);
      setLaws((cur) => cur.filter((l) => l.law_name !== lawName));
    } catch (e) {
      const msg = e instanceof Error ? e.message : "删除失败";
      toast.error(msg);
    } finally {
      setDeleting(null);
    }
  }

  const totalChunks = laws.reduce((s, l) => s + l.chunk_count, 0);

  return (
    <div
      className="overflow-hidden rounded-[20px]"
      style={{
        background: "rgba(255, 255, 255, 0.03)",
        border: "1px solid rgba(255, 255, 255, 0.06)",
        backdropFilter: "blur(10px)",
        WebkitBackdropFilter: "blur(10px)",
      }}
    >
      {/* 表头 */}
      <div className="flex items-center justify-between border-b border-white/[0.06] px-5 py-3.5">
        <div>
          <p className="text-[13.5px] font-medium text-[var(--color-fg)]">
            已入库法律
          </p>
          <p className="text-[11.5px] text-[var(--color-fg-faint)]">
            共 {laws.length} 部法律 · {totalChunks.toLocaleString()} 条法条
          </p>
        </div>
        <button
          onClick={reload}
          disabled={loading}
          className={cn(
            "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-xs transition-colors",
            "text-[var(--color-fg-muted)] hover:bg-white/[0.05] hover:text-[var(--color-fg)]",
          )}
        >
          {loading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <RefreshCw className="h-3.5 w-3.5" />
          )}
          刷新
        </button>
      </div>

      {/* 内容区 */}
      {loading && laws.length === 0 ? (
        <div className="px-4 py-10 text-center text-sm text-[var(--color-fg-faint)]">
          <Loader2 className="mx-auto mb-2 h-4 w-4 animate-spin" />
          加载中…
        </div>
      ) : laws.length === 0 ? (
        <div className="flex flex-col items-center gap-2 px-4 py-12 text-center text-sm text-[var(--color-fg-faint)]">
          <BookOpen className="h-8 w-8 opacity-30" />
          <p>暂无法律入库，先在上方上传 .docx 文件。</p>
        </div>
      ) : (
        <ul>
          <AnimatePresence initial={false}>
            {laws.map((law) => (
              <motion.li
                key={law.law_name}
                layout
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, height: 0 }}
                className="group flex items-center gap-3 border-b border-white/[0.04] px-5 py-3 text-sm last:border-b-0 transition-colors hover:bg-white/[0.03]"
              >
                {/* 可点击区域：图标 + 法律名称 */}
                <button
                  type="button"
                  onClick={() => setActiveLaw(law.law_name)}
                  className="flex min-w-0 flex-1 items-center gap-3 text-left"
                  aria-label={`查看《${law.law_name}》条文详情`}
                >
                  <Scale className="h-4 w-4 shrink-0 text-emerald-500/60 transition-colors group-hover:text-emerald-400" />
                  <span
                    className="min-w-0 flex-1 truncate text-[13.5px] text-[var(--color-fg)] transition-colors group-hover:text-emerald-300"
                    title={law.law_name}
                  >
                    《{law.law_name}》
                  </span>
                </button>

                {/* 生效日期 */}
                {law.effective_date && (
                  <span className="shrink-0 text-[11px] text-[var(--color-fg-faint)]">
                    {law.effective_date}
                  </span>
                )}

                {/* 法条数量 */}
                <span className="shrink-0 rounded-full bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-400">
                  {law.chunk_count} 条
                </span>

                {/* 删除按钮 */}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    void handleDelete(law.law_name);
                  }}
                  disabled={deleting === law.law_name}
                  aria-label={`删除《${law.law_name}》`}
                  className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-[var(--color-fg-faint)] transition-colors hover:bg-[rgba(255,107,107,0.10)] hover:text-[var(--color-destructive)]"
                >
                  {deleting === law.law_name ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Trash2 className="h-3.5 w-3.5" />
                  )}
                </button>
              </motion.li>
            ))}
          </AnimatePresence>
        </ul>
      )}

      <LawChunkDetailDialog
        lawName={activeLaw}
        onOpenChange={(open) => {
          if (!open) setActiveLaw(null);
        }}
      />
    </div>
  );
}
