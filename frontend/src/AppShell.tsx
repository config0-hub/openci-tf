// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { AuthGate } from "./AuthGate";
import { TOKEN_CHANGED, TOKEN_KEY } from "./api";

const tabs = [
  { label: "RUNS", to: "/" },
  { label: "PIPELINES", to: "/pipelines" },
  { label: "REPOS", to: "/repos" },
  { label: "ACCOUNTS", to: "/accounts" },
  { label: "LOCKS", to: "/locks" },
  { label: "GATES", to: "/gates" },
] as const;

function titleFor(pathname: string): string {
  if (pathname === "/") return "RUNS";
  if (pathname === "/runs/new") return "NEW RUN";
  if (pathname.startsWith("/runs/")) return "RUN DETAIL";
  const section = tabs.find((tab) => tab.to === pathname)?.label;
  return section ?? "CONSOLE";
}

export function AppShell() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const queryClient = useQueryClient();

  useEffect(() => {
    document.title = `${titleFor(pathname)} · OPENCI-TF CONSOLE`;
  }, [pathname]);

  function signOut() {
    queryClient.clear();
    sessionStorage.removeItem(TOKEN_KEY);
    window.dispatchEvent(new Event(TOKEN_CHANGED));
  }

  return (
    <AuthGate>
      <div className="console-shell">
        <nav className="tab-bank" aria-label="Primary console procedures">
          <div className="bank-mark" aria-hidden="true">IAC<br />CI</div>
          {tabs.map((tab) => {
            const active = tab.to === "/" ? pathname === "/" || pathname.startsWith("/runs/") : pathname === tab.to;
            return (
              <Link
                key={tab.to}
                to={tab.to}
                className="edge-tab"
                data-active={active || undefined}
                aria-current={active ? "page" : undefined}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>
        <div className="paper-ground">
          <header className="masthead">
            <Link to="/" className="wordmark">OPENCI-TF · QUICK REFERENCE</Link>
            <div className="masthead-meta">
              <span>RUN REGISTRY · BOUNDED RETENTION</span>
              <button type="button" className="text-action" onClick={signOut}>CLEAR TOKEN</button>
            </div>
          </header>
          <Outlet />
          <footer className="page-footer">
            PROCEDURE RECORDS EXPIRE AT THE API-SUPPLIED RETENTION TIME · TIMES SHOWN UTC
          </footer>
        </div>
      </div>
    </AuthGate>
  );
}
