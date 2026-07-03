"use client";

import * as React from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

interface MarkdownProps {
  content: string;
  className?: string;
}

// Wrap wide tables in a horizontally-scrollable container so they never
// overflow the chat bubble. The wrapper is itself constrained by the
// bubble's max-width.
const components: Components = {
  table: ({ node, ...props }) => (
    <div className="markdown-table-wrapper">
      <table {...props} />
    </div>
  ),
};

// Disable strikethrough: LLMs occasionally emit ~~text~~ for emphasis or
// comparison purposes, which renders as unwanted <del> strike-through.
const remarkGfmOptions = { strikethrough: false };

// 折叠 3 行以上连续换行为一个空行：模型（尤其推理内容）常吐出多余空行，
// react-markdown 会把它们渲染成多个空段落，造成大片视觉空白。
function normalizeMarkdown(text: string): string {
  return (text || "")
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+\n/g, "\n") // 去掉行尾空白
    .replace(/\n{3,}/g, "\n\n") // 连续空行折叠
    .trim();
}

export function Markdown({ content, className }: MarkdownProps) {
  return (
    <div className={cn("markdown-body", className)}>
      <ReactMarkdown
        remarkPlugins={[[remarkGfm, remarkGfmOptions]]}
        components={components}
      >
        {normalizeMarkdown(content)}
      </ReactMarkdown>
    </div>
  );
}
