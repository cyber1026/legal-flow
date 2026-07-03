"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "flex h-10 w-full rounded-xl border border-[var(--color-border)] bg-[var(--color-bg-subtle)] px-3.5 py-1.5 text-sm text-[var(--color-fg)] transition-all",
        "placeholder:text-[var(--color-fg-faint)] file:border-0 file:bg-transparent file:text-sm file:font-medium",
        "focus-visible:outline-none focus-visible:border-[color-mix(in_oklch,var(--color-brand)_55%,var(--color-border))] focus-visible:ring-4 focus-visible:ring-[var(--color-brand-soft)]",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";
