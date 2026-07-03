"use client";

import * as React from "react";
import { motion } from "framer-motion";
import { Loader2, UploadCloud, FileCheck, FileWarning, Scale } from "lucide-react";
import { toast } from "sonner";
import { apiFetch } from "@/lib/api";
import type { LawIngestJob } from "@/lib/types";
import { cn } from "@/lib/utils";

interface LawUploaderProps {
  onComplete?: (job: LawIngestJob) => void;
}

interface ActiveJob {
  job: LawIngestJob;
  filename: string;
}

/** 三阶段进度标签 */
function statusLabel(job: LawIngestJob): string {
  switch (job.status) {
    case "pending":
      return "排队中";
    case "parsing":
      return "Docling 解析中…";
    case "embedding":
      return job.parsed_chunks != null
        ? `向量化中（共 ${job.parsed_chunks} 条）…`
        : "向量化中…";
    case "done":
      return `完成 · ${job.embedded_chunks ?? 0} 条法条已入库`;
    case "failed":
      return `失败：${job.error || "未知错误"}`;
    default:
      return job.status;
  }
}

export function LawUploader({ onComplete }: LawUploaderProps) {
  const [dragOver, setDragOver] = React.useState(false);
  const [active, setActive] = React.useState<ActiveJob[]>([]);
  const inputRef = React.useRef<HTMLInputElement>(null);

  function patchJob(jobId: string, patch: Partial<LawIngestJob>) {
    setActive((cur) =>
      cur.map((a) =>
        a.job.job_id === jobId ? { ...a, job: { ...a.job, ...patch } } : a,
      ),
    );
  }

  async function pollJob(jobId: string) {
    while (true) {
      try {
        const resp = await apiFetch(`/law-ingest/jobs/${jobId}`);
        if (!resp.ok) throw new Error(`status ${resp.status}`);
        const job = (await resp.json()) as LawIngestJob;
        patchJob(jobId, job);
        if (job.status === "done" || job.status === "failed") {
          if (job.status === "done") {
            const lawName = job.law_name ? `《${job.law_name}》` : job.filename;
            toast.success(`${lawName} 入库完成（${job.embedded_chunks ?? 0} 条法条）`);
          } else {
            toast.error(`${job.filename} 入库失败：${job.error || "未知错误"}`);
          }
          onComplete?.(job);
          setTimeout(() => {
            setActive((cur) => cur.filter((a) => a.job.job_id !== jobId));
          }, 5000);
          return;
        }
      } catch {
        toast.error("查询法律入库任务状态失败");
        return;
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
  }

  async function handleFiles(files: FileList | File[] | null) {
    if (!files) return;
    const arr = Array.from(files).filter((f) =>
      f.name.toLowerCase().endsWith(".docx"),
    );
    if (arr.length === 0) {
      toast.error("法律文件仅支持 .docx 格式");
      return;
    }
    for (const file of arr) {
      try {
        const fd = new FormData();
        fd.append("file", file);
        const resp = await apiFetch("/law-ingest", { method: "POST", body: fd });
        if (!resp.ok) {
          const txt = await resp.text();
          throw new Error(txt || `status ${resp.status}`);
        }
        const job = (await resp.json()) as LawIngestJob;
        setActive((cur) => [...cur, { job, filename: file.name }]);
        toast(`已开始处理 ${file.name}`, {
          description: "正在用 Docling 解析法条结构，随后向量化入库…",
        });
        void pollJob(job.job_id);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "上传失败";
        toast.error(`${file.name} 上传失败：${msg}`);
      }
    }
  }

  return (
    <div className="space-y-3">
      {/* 拖拽上传区 */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          void handleFiles(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center gap-3 rounded-[20px] p-10 text-center text-sm transition-all duration-200",
        )}
        style={{
          background: dragOver
            ? "rgba(52, 211, 153, 0.06)"
            : "rgba(255, 255, 255, 0.03)",
          border: dragOver
            ? "1.5px dashed rgba(52, 211, 153, 0.6)"
            : "1.5px dashed rgba(255, 255, 255, 0.10)",
          backdropFilter: "blur(10px)",
          WebkitBackdropFilter: "blur(10px)",
          boxShadow: dragOver
            ? "0 0 0 4px rgba(52, 211, 153, 0.10)"
            : "none",
        }}
      >
        <div
          className="flex h-12 w-12 items-center justify-center rounded-2xl text-white"
          style={{
            background: "linear-gradient(135deg, #34d399 0%, #059669 100%)",
            boxShadow: "0 8px 24px -8px rgba(52, 211, 153, 0.5)",
          }}
        >
          <Scale className="h-5 w-5" />
        </div>
        <div className="space-y-1">
          <p className="text-[14.5px] font-medium text-[var(--color-fg)]">
            拖拽.docx 文件至此处，或点击选择
          </p>
          <p className="text-[12.5px] text-[var(--color-fg-muted)]">
          支持多法律文件上传，自动解析法条结构，智能完成向量化与入库，快速构建法律知识库。
          </p>
        </div>
        <input
          ref={inputRef}
          type="file"
          accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          multiple
          className="hidden"
          onChange={(e) => {
            void handleFiles(e.target.files);
            if (inputRef.current) inputRef.current.value = "";
          }}
        />
      </div>

      {/* 进度列表 */}
      {active.length > 0 && (
        <ul className="space-y-1.5">
          {active.map(({ job, filename }) => (
            <motion.li
              key={job.job_id}
              layout
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              className="flex items-center gap-3 rounded-xl px-4 py-2.5 text-xs"
              style={{
                background: "rgba(255, 255, 255, 0.03)",
                border: "1px solid rgba(255, 255, 255, 0.06)",
                backdropFilter: "blur(10px)",
                WebkitBackdropFilter: "blur(10px)",
              }}
            >
              {/* 状态图标 */}
              <span className="flex h-5 w-5 shrink-0 items-center justify-center">
                {job.status === "done" ? (
                  <FileCheck className="h-4 w-4 text-emerald-400" />
                ) : job.status === "failed" ? (
                  <FileWarning className="h-4 w-4 text-[var(--color-destructive)]" />
                ) : (
                  <Loader2 className="h-4 w-4 animate-spin text-emerald-400" />
                )}
              </span>

              {/* 文件名（优先显示法律名） */}
              <span className="min-w-0 flex-1 truncate font-medium text-[var(--color-fg)]">
                {job.law_name ? `《${job.law_name}》` : filename}
              </span>

              {/* 阶段进度标签 */}
              <span
                className={cn(
                  "shrink-0 text-[var(--color-fg-muted)]",
                  job.status === "failed" && "text-[var(--color-destructive)]",
                  job.status === "done" && "text-emerald-400",
                )}
              >
                {statusLabel(job)}
              </span>
            </motion.li>
          ))}
        </ul>
      )}
    </div>
  );
}
