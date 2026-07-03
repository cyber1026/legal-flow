"use client";

import * as React from "react";
import { FileWarning } from "lucide-react";
import { useContractReviewStore } from "@/lib/contract-review-store";
import { getToken } from "@/lib/api";

export function OriginalPreview() {
  const summary = useContractReviewStore((s) => s.summary);
  const setActiveTab = useContractReviewStore((s) => s.setActiveTab);

  if (!summary) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-[var(--color-fg-faint)]">
        等待上传…
      </div>
    );
  }

  const docType = summary.doc_type;
  const token = getToken();
  const fileUrl = token
    ? `/api/contract-review/contracts/${summary.id}/file?token=${encodeURIComponent(token)}`
    : `/api/contract-review/contracts/${summary.id}/file`;

  if (docType === "docx") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-4 px-8 text-center">
        <FileWarning className="h-10 w-10 text-[var(--color-fg-faint)]" />
        <div>
          <p className="text-sm font-medium text-[var(--color-fg-muted)]">
            DOCX 暂不支持原文预览
          </p>
          <p className="mt-1 text-[12.5px] text-[var(--color-fg-faint)]">
            请切换到「条款视图」查看解析后的合同内容
          </p>
        </div>
        <button
          onClick={() => setActiveTab("clause")}
          className="mt-1 rounded-lg bg-[var(--color-brand-soft)] px-4 py-1.5 text-[13px] font-medium text-[var(--color-brand)] transition-colors hover:bg-[var(--color-brand)]/20"
        >
          前往条款视图
        </button>
      </div>
    );
  }

  if (docType === "pdf") {
    return <PdfPreview url={fileUrl} />;
  }

  // Image types
  return <ImagePreview url={fileUrl} alt={summary.filename} />;
}

function PdfPreview({ url }: { url: string }) {
  return (
    <iframe
      src={url}
      title="合同原文"
      className="h-full w-full border-0"
      style={{ background: "rgba(255,255,255,0.02)" }}
    />
  );
}

function ImagePreview({ url, alt }: { url: string; alt: string }) {
  const [scale, setScale] = React.useState(1);

  return (
    <div
      className="flex h-full items-start justify-center overflow-auto p-4"
      onWheel={(e) => {
        if (e.ctrlKey || e.metaKey) {
          e.preventDefault();
          setScale((s) => Math.max(0.25, Math.min(4, s + (e.deltaY > 0 ? -0.1 : 0.1))));
        }
      }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={url}
        alt={alt}
        className="max-w-full rounded-lg"
        style={{
          transform: `scale(${scale})`,
          transformOrigin: "top center",
          transition: "transform 0.15s ease",
        }}
      />
    </div>
  );
}
