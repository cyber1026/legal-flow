"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { Sidebar } from "./session-list";

const APP_ROUTES = new Set(["/", "/knowledge", "/settings"]);
const APP_ROUTE_PREFIXES = ["/c/"];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const isAppRoute =
    APP_ROUTES.has(pathname) ||
    APP_ROUTE_PREFIXES.some((p) => pathname.startsWith(p));

  if (!isAppRoute) {
    return <>{children}</>;
  }

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-[var(--color-bg)]">
      <Sidebar />
      <main className="relative min-w-0 flex-1">{children}</main>
    </div>
  );
}
