"use client";

import * as React from "react";
import { AnimatePresence, motion } from "framer-motion";
import { ArrowUp, FileText, Loader2, Paperclip, Square, X } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

interface ChatInputProps {
  onSend: (content: string, images?: string[]) => void;
  onStop?: () => void;
  onContractUpload?: (file: File) => void;
  contractUploading?: boolean;
  streaming?: boolean;
  disabled?: boolean;
  placeholder?: string;
}

// Keep payload sane.  Each base64 image roughly inflates by ~33%.
const MAX_IMAGES = 6;
const MAX_BYTES_PER_IMAGE = 5 * 1024 * 1024;

function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as string);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

const CONTRACT_ACCEPT =
  ".pdf,.docx,.png,.jpg,.jpeg,.bmp,.webp,.tif,.tiff";

export function ChatInput({
  onSend,
  onStop,
  onContractUpload,
  contractUploading,
  streaming,
  disabled,
  placeholder = "向 Agent 提问，或粘贴/拖拽图片…",
}: ChatInputProps) {
  const [value, setValue] = React.useState("");
  const [focused, setFocused] = React.useState(false);
  const [multiLine, setMultiLine] = React.useState(false);
  const [images, setImages] = React.useState<string[]>([]);
  const [dragOver, setDragOver] = React.useState(false);
  const ref = React.useRef<HTMLTextAreaElement>(null);
  const fileRef = React.useRef<HTMLInputElement>(null);
  const contractFileRef = React.useRef<HTMLInputElement>(null);

  const SINGLE_LINE_HEIGHT = 22;

  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const next = Math.min(el.scrollHeight, 200);
    el.style.height = `${next}px`;
    setMultiLine(next > SINGLE_LINE_HEIGHT + 2 || images.length > 0);
  }, [value, images.length]);

  // Reset attached images after the parent confirms streaming starts.
  const prevStreamingRef = React.useRef(false);
  React.useEffect(() => {
    if (streaming && !prevStreamingRef.current) {
      // Streaming has just kicked off; the message was sent, clear state.
      setImages([]);
    }
    prevStreamingRef.current = !!streaming;
  }, [streaming]);

  async function ingestFiles(files: FileList | File[] | null | undefined) {
    if (!files) return;
    const arr = Array.from(files).filter((f) => f.type.startsWith("image/"));
    if (arr.length === 0) return;

    let added = 0;
    const next: string[] = [];
    for (const file of arr) {
      if (images.length + next.length >= MAX_IMAGES) {
        toast.warning(`最多 ${MAX_IMAGES} 张图片`);
        break;
      }
      if (file.size > MAX_BYTES_PER_IMAGE) {
        toast.error(`${file.name || "图片"} 超过 5MB`);
        continue;
      }
      try {
        const url = await fileToDataUrl(file);
        next.push(url);
        added += 1;
      } catch {
        toast.error("读取图片失败");
      }
    }
    if (next.length) setImages((cur) => [...cur, ...next]);
    if (added) toast.success(`已添加 ${added} 张图片`);
  }

  function handleSubmit(e?: React.FormEvent) {
    e?.preventDefault();
    const v = value.trim();
    if (!v && images.length === 0) return;
    if (streaming) return;
    onSend(v, images.length ? images : undefined);
    setValue("");
    // images are cleared by the streaming useEffect; clear here too for instant
    // visual feedback in case streaming flips synchronously.
    setImages([]);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      if (!streaming) handleSubmit();
    }
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    if (!e.clipboardData) return;
    const items = Array.from(e.clipboardData.items);
    const imageFiles = items
      .filter((it) => it.kind === "file" && it.type.startsWith("image/"))
      .map((it) => it.getAsFile())
      .filter((f): f is File => !!f);
    if (imageFiles.length > 0) {
      e.preventDefault();
      void ingestFiles(imageFiles);
    }
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer?.files?.length) {
      void ingestFiles(e.dataTransfer.files);
    }
  }

  function removeImage(idx: number) {
    setImages((cur) => cur.filter((_, i) => i !== idx));
  }

  const canSend =
    !streaming && !disabled && (value.trim().length > 0 || images.length > 0);

  return (
    <form
      onSubmit={handleSubmit}
      onDragOver={(e) => {
        if (e.dataTransfer?.types?.includes("Files")) {
          e.preventDefault();
          setDragOver(true);
        }
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
      className={cn(
        "relative flex flex-col gap-2 px-5 py-3.5 transition-all duration-200",
      )}
      style={{
        background: dragOver
          ? "rgba(124, 140, 255, 0.08)"
          : "rgba(255, 255, 255, 0.04)",
        border: dragOver
          ? "1px dashed rgba(124, 140, 255, 0.6)"
          : focused
            ? "1px solid rgba(124, 140, 255, 0.55)"
            : "1px solid rgba(255, 255, 255, 0.08)",
        borderRadius: "24px",
        minHeight: "64px",
        boxShadow: focused
          ? "0 0 0 4px rgba(124, 140, 255, 0.12), 0 8px 32px -8px rgba(124, 140, 255, 0.25)"
          : "0 4px 24px -8px rgba(0, 0, 0, 0.3)",
        backdropFilter: "blur(10px)",
        WebkitBackdropFilter: "blur(10px)",
      }}
    >
      {/* Thumbnail row */}
      <AnimatePresence initial={false}>
        {images.length > 0 && (
          <motion.div
            key="thumbs"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.18, ease: [0.2, 0.7, 0.2, 1] }}
            className="flex flex-wrap gap-2 overflow-hidden"
          >
            {images.map((url, i) => (
              <Thumbnail key={i} url={url} onRemove={() => removeImage(i)} />
            ))}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Input row */}
      <div className={cn("flex gap-2", multiLine ? "items-end" : "items-center")}>
        <button
          type="button"
          onClick={() => fileRef.current?.click()}
          aria-label="上传图片"
          className="flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full text-[var(--color-fg-muted)] transition-all hover:bg-white/[0.06] hover:text-[var(--color-fg)]"
        >
          <Paperclip className="h-4 w-4" />
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => {
            void ingestFiles(e.target.files);
            if (fileRef.current) fileRef.current.value = "";
          }}
        />

        {onContractUpload && (
          <>
            <button
              type="button"
              onClick={() => contractFileRef.current?.click()}
              disabled={contractUploading}
              aria-label="上传合同"
              title="上传合同"
              className={cn(
                "flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition-all",
                contractUploading
                  ? "cursor-wait text-[var(--color-brand)]"
                  : "cursor-pointer text-[var(--color-fg-muted)] hover:bg-white/[0.06] hover:text-emerald-400",
              )}
            >
              {contractUploading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <FileText className="h-4 w-4" />
              )}
            </button>
            <input
              ref={contractFileRef}
              type="file"
              accept={CONTRACT_ACCEPT}
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) onContractUpload(file);
                if (contractFileRef.current) contractFileRef.current.value = "";
              }}
            />
          </>
        )}

        <textarea
          ref={ref}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={placeholder}
          disabled={disabled}
          rows={1}
          className={cn(
            "m-0 block flex-1 resize-none bg-transparent p-0 text-[15px] leading-[22px] text-[var(--color-fg)] outline-none",
            "placeholder:text-[var(--color-fg-faint)]",
            "max-h-[200px] min-h-[22px]",
          )}
        />

        {streaming ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="停止生成"
            className="flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-full transition-all hover:scale-105"
            style={{
              background: "rgba(255, 255, 255, 0.08)",
              border: "1px solid rgba(255, 255, 255, 0.12)",
            }}
          >
            <Square className="h-3.5 w-3.5 fill-current text-[var(--color-fg)]" />
          </button>
        ) : (
          <button
            type="submit"
            aria-label="发送"
            disabled={!canSend}
            className={cn(
              "flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition-all",
              canSend ? "cursor-pointer hover:scale-105" : "cursor-not-allowed",
            )}
            style={{
              background: canSend
                ? "linear-gradient(135deg, #7C8CFF 0%, #6878FF 100%)"
                : "rgba(255, 255, 255, 0.06)",
              color: canSend ? "#ffffff" : "rgba(255, 255, 255, 0.3)",
              boxShadow: canSend
                ? "0 4px 18px -4px rgba(124, 140, 255, 0.55)"
                : "none",
            }}
          >
            <ArrowUp className="h-4 w-4" />
          </button>
        )}
      </div>
    </form>
  );
}

function Thumbnail({ url, onRemove }: { url: string; onRemove: () => void }) {
  return (
    <motion.div
      layout
      initial={{ opacity: 0, scale: 0.92 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.92 }}
      transition={{ duration: 0.18 }}
      className="group relative h-16 w-16 overflow-hidden rounded-xl"
      style={{
        background: "rgba(255,255,255,0.04)",
        border: "1px solid rgba(255,255,255,0.10)",
      }}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={url} alt="upload" className="h-full w-full object-cover" />
      <button
        type="button"
        onClick={onRemove}
        aria-label="移除"
        className="absolute right-1 top-1 flex h-5 w-5 items-center justify-center rounded-full bg-black/60 text-white opacity-0 transition-opacity group-hover:opacity-100"
      >
        <X className="h-3 w-3" />
      </button>
    </motion.div>
  );
}
