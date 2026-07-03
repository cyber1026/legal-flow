"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => (
  <textarea
    ref={ref}
    className={cn(
      "flex min-h-[60px] w-full rounded-md border border-[var(--color-input)] bg-[var(--color-bg)] px-3 py-2 text-sm text-[var(--color-fg)] shadow-sm transition-colors",
      "placeholder:text-[var(--color-fg-muted)]",
      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-ring)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--color-bg)]",
      "resize-none",
      className,
    )}
    {...props}
  />
));
Textarea.displayName = "Textarea";
