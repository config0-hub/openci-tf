// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, probeAuthorization, TOKEN_CHANGED, TOKEN_KEY, TOKEN_REJECTED } from "./api";

export function AuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [token, setToken] = useState(() => sessionStorage.getItem(TOKEN_KEY) ?? "");
  const [tokenGeneration, setTokenGeneration] = useState(0);
  const [rejected, setRejected] = useState(false);
  const tokenInput = useRef<HTMLInputElement>(null);
  const tokenRef = useRef(token);
  const authorization = useQuery({
    queryKey: ["authorization-probe", tokenGeneration],
    queryFn: probeAuthorization,
    enabled: Boolean(token),
    retry: false,
    staleTime: Number.POSITIVE_INFINITY,
  });

  useEffect(() => {
    const update = () => {
      const next = sessionStorage.getItem(TOKEN_KEY) ?? "";
      if (next === tokenRef.current) return;
      queryClient.clear();
      tokenRef.current = next;
      setToken(next);
      setTokenGeneration((generation) => generation + 1);
    };
    const reject = () => {
      queryClient.clear();
      setRejected(true);
    };
    window.addEventListener(TOKEN_CHANGED, update);
    window.addEventListener(TOKEN_REJECTED, reject);
    return () => {
      window.removeEventListener(TOKEN_CHANGED, update);
      window.removeEventListener(TOKEN_REJECTED, reject);
    };
  }, [queryClient]);

  useEffect(() => {
    if (rejected) tokenInput.current?.focus();
  }, [rejected]);

  function clearToken() {
    queryClient.clear();
    sessionStorage.removeItem(TOKEN_KEY);
    tokenRef.current = "";
    setToken("");
    setTokenGeneration((generation) => generation + 1);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next = String(data.get("token") ?? "");
    if (!next) return;
    queryClient.clear();
    setRejected(false);
    sessionStorage.setItem(TOKEN_KEY, next);
    tokenRef.current = next;
    setToken(next);
    setTokenGeneration((generation) => generation + 1);
  }

  if (token && authorization.isSuccess) return children;

  if (token) {
    return (
      <main className="auth-procedure">
        <article className="auth-page" aria-labelledby="auth-title">
          <header className="page-heading auth-heading">
            <div>
              <div className="procedure-code">ACCESS CONTROL MEMORY ITEM</div>
              <h1 id="auth-title">VERIFYING CONSOLE ACCESS</h1>
            </div>
            <div className="retention-stamp">AUTH<br />PROBE</div>
          </header>
          {authorization.isError ? (
            <section className="abnormal-block auth-rejection" role="alert">
              <div className="procedure-code">ABNORMAL PROCEDURE · AUTHORIZATION PROBE</div>
              <h2>CONSOLE ACCESS NOT VERIFIED</h2>
              <pre>{authorization.error instanceof ApiError
                ? `HTTP ${authorization.error.status}\n${authorization.error.message}`
                : authorization.error.message}</pre>
              <div className="auth-probe-actions">
                <button type="button" onClick={() => void authorization.refetch()}>RETRY AUTHORIZATION PROBE</button>
                <button type="button" onClick={clearToken}>CLEAR TOKEN</button>
              </div>
            </section>
          ) : (
            <div className="detail-loading" role="status" aria-live="polite">
              <span aria-hidden="true" />VERIFYING TOKEN WITH THE RUN REGISTRY
            </div>
          )}
        </article>
      </main>
    );
  }

  return (
    <main className="auth-procedure">
      <article className="auth-page" aria-labelledby="auth-title">
        <header className="page-heading auth-heading">
          <div>
            <div className="procedure-code">ACCESS CONTROL MEMORY ITEM</div>
            <h1 id="auth-title">CONSOLE ACCESS REQUIRED</h1>
          </div>
          <div className="retention-stamp">TAB<br />SESSION</div>
        </header>

        <p id="auth-guidance" className="fixed-measure auth-guidance">
          PRESENT THE SHARED CONSOLE BEARER TOKEN. IT REMAINS IN THIS BROWSER TAB ONLY
          AND IS NEVER PRINTED BACK TO THE PAGE.
        </p>

        {rejected && (
          <section id="auth-rejection" className="abnormal-block auth-rejection" role="alert">
            <div className="procedure-code">ABNORMAL PROCEDURE · ACCESS DENIED</div>
            <h2>TOKEN REJECTED</h2>
            <p>VERIFY THE SHARED CONSOLE TOKEN, THEN COMPLETE THE MEMORY ITEM AGAIN.</p>
          </section>
        )}

        <form onSubmit={submit} className="memory-item auth-form" aria-label="Console token challenge">
          <div className="auth-challenge">
            <label htmlFor="console-token">CHALLENGE · CONSOLE TOKEN</label>
            <strong>RESPONSE REQUIRED</strong>
          </div>
          <input
            ref={tokenInput}
            id="console-token"
            name="token"
            type="password"
            autoComplete="off"
            required
            aria-invalid={rejected || undefined}
            aria-describedby={rejected ? "auth-rejection" : "auth-guidance"}
          />
          <button type="submit">RESPONSE · ENTER CONSOLE</button>
        </form>

        <p className="auth-note">AUTHORIZATION IS VERIFIED BY THE FIRST API PROCEDURE · THE TOKEN IS NEVER ECHOED</p>
      </article>
    </main>
  );
}
