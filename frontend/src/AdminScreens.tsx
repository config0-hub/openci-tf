// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type ReactNode, useEffect, useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import type { AccountTarget, ActiveLock, GatesResponse, RepoRegistration } from "./admin-contract";
import { ApiError, getGates, listAccounts, listLocks, listRepos, responseClockOffset } from "./api";
import { advanceCursor, CursorPagination, retreatCursor, type CursorSearch } from "./Pagination";

const utc = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  timeZone: "UTC",
  hour12: false,
});

function PageHeading({ code, title }: { code: string; title: string }) {
  return (
    <header className="page-heading">
      <div>
        <div className="procedure-code">{code}</div>
        <h1>{title}</h1>
      </div>
      <div className="retention-stamp">REFERENCE<br />ONLY</div>
    </header>
  );
}

function ReferenceState<T>({
  query,
  hasItems,
  emptyTitle,
  emptyNote,
  partialEmpty,
  children,
}: {
  query: UseQueryResult<T, Error>;
  hasItems: (data: T) => boolean;
  emptyTitle: string;
  emptyNote: string;
  partialEmpty?: boolean;
  children: (data: T) => ReactNode;
}) {
  if (query.isPending) {
    return (
      <div className="reference-list loading-list" role="status" aria-label="Drawing reference rows" aria-live="polite">
        {[1, 2, 3].map((row) => (
          <div className="loading-row" key={row}><span>{String(row).padStart(2, "0")}</span><i /><span>DRAWING</span></div>
        ))}
      </div>
    );
  }

  if (query.isError) {
    const unavailable = query.error instanceof ApiError && query.error.status === 404;
    return (
      <section className="abnormal-block" role="alert">
        <div className="procedure-code">ABNORMAL PROCEDURE · HTTP FAILURE</div>
        <h2>{unavailable ? "REFERENCE ROUTE NOT YET AVAILABLE" : "REFERENCE PROCEDURE UNAVAILABLE"}</h2>
        <pre>{query.error instanceof ApiError ? `HTTP ${query.error.status}\n${query.error.message}` : query.error.message}</pre>
        <button type="button" onClick={() => void query.refetch()}>RETRY REFERENCE ITEM</button>
      </section>
    );
  }

  if (!hasItems(query.data)) {
    return (
      <section className="empty-procedure">
        <strong>{partialEmpty ? "NO ROWS FILED ON THIS BOUNDED PAGE" : emptyTitle}</strong>
        <span>{partialEmpty ? "THIS PAGE IS ONLY ONE CURSORED SLICE. REVIEW THE ADJACENT BOUNDED PAGES." : emptyNote}</span>
      </section>
    );
  }

  return children(query.data);
}

function BoolState({ value, trueWord, falseWord }: { value: boolean; trueWord: string; falseWord: string }) {
  const word = value ? trueWord : falseWord;
  return <span className="boolean-state" data-enabled={value || undefined} aria-label={`State: ${word}`}><b aria-hidden="true">{value ? "✓" : "×"}</b>{word}</span>;
}

function Pagination({
  search,
  nextCursor,
  label,
  navigate,
}: {
  search: CursorSearch;
  nextCursor?: string;
  label: string;
  navigate: (search: CursorSearch) => void;
}) {
  return (
    <CursorPagination
      page={(search.history?.length ?? 0) + 1}
      hasNext={Boolean(nextCursor)}
      hasPrevious={Boolean(search.history?.length)}
      label={label}
      onNext={() => {
        if (nextCursor) navigate(advanceCursor(search, nextCursor));
      }}
      onPrevious={() => navigate(retreatCursor(search))}
    />
  );
}

export function RepoRow({ repo, index }: { repo: RepoRegistration; index: number }) {
  return (
    <li className="reference-row">
      <span className="row-number">{String(index + 1).padStart(2, "0")}</span>
      <div className="reference-subject"><strong>{repo.repo_name}</strong><span>REGISTERED REPOSITORY</span></div>
      <dl className="reference-facts">
        <div><dt>TRIGGER IDS</dt><i aria-hidden="true" /><dd>{repo.trigger_ids.join(" · ")}</dd></div>
        <div><dt>REVIEW APPROVAL</dt><i aria-hidden="true" /><dd><BoolState value={repo.require_approval} trueWord="REQUIRED" falseWord="NOT REQUIRED" /></dd></div>
      </dl>
    </li>
  );
}

export function ReposPage() {
  const search = useSearch({ from: "/repos" });
  const navigate = useNavigate({ from: "/repos" });
  const query = useQuery({ queryKey: ["repos", search.cursor], queryFn: () => listRepos(search.cursor) });
  return (
    <main className="procedure-page reference-page">
      <PageHeading code="REGISTRATION REFERENCE" title="REGISTERED REPOS" />
      <ReferenceState
        query={query}
        hasItems={(data) => data.repos.length > 0}
        partialEmpty={Boolean(search.cursor || query.data?.cursor)}
        emptyTitle="NO REPOSITORIES REGISTERED"
        emptyNote="REPOSITORY SETTINGS APPEAR AFTER AN OPERATOR FILES A REGISTRATION."
      >
        {(data) => <ol className="reference-list">{data.repos.map((repo, index) => <RepoRow key={`${repo.repo_name}:${repo.trigger_ids.join(":")}`} repo={repo} index={index} />)}</ol>}
      </ReferenceState>
      {!query.isPending && !query.isError && (
        <Pagination search={search} nextCursor={query.data.cursor} label="Repository reference" navigate={(next) => void navigate({ search: next })} />
      )}
    </main>
  );
}

export function AccountRow({ account, index }: { account: AccountTarget; index: number }) {
  return (
    <li className="reference-row account-row">
      <span className="row-number">{String(index + 1).padStart(2, "0")}</span>
      <div className="reference-subject"><strong>{account.alias}</strong><span>ACCOUNT {account.account_id}</span></div>
      <dl className="reference-facts">
        <div><dt>ROLE NAME</dt><i aria-hidden="true" /><dd>{account.role_name}</dd></div>
      </dl>
    </li>
  );
}

export function AccountsPage() {
  const search = useSearch({ from: "/accounts" });
  const navigate = useNavigate({ from: "/accounts" });
  const query = useQuery({ queryKey: ["accounts", search.cursor], queryFn: () => listAccounts(search.cursor) });
  return (
    <main className="procedure-page reference-page">
      <PageHeading code="TARGET REFERENCE" title="ACCOUNTS / TARGETS" />
      <ReferenceState
        query={query}
        hasItems={(data) => data.accounts.length > 0}
        partialEmpty={Boolean(search.cursor || query.data?.cursor)}
        emptyTitle="NO ACCOUNTS OR TARGETS REGISTERED"
        emptyNote="ROLE NAME REFERENCES APPEAR AFTER ACCOUNT REGISTRATION."
      >
        {(data) => <ol className="reference-list">{data.accounts.map((account, index) => <AccountRow key={`${account.alias}:${account.account_id}`} account={account} index={index} />)}</ol>}
      </ReferenceState>
      {!query.isPending && !query.isError && (
        <Pagination search={search} nextCursor={query.data.cursor} label="Account reference" navigate={(next) => void navigate({ search: next })} />
      )}
    </main>
  );
}

function useClock(offsetMs: number | undefined): number | undefined {
  const [clientNow, setClientNow] = useState(Date.now);
  useEffect(() => {
    const interval = window.setInterval(() => setClientNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, []);
  return offsetMs === undefined ? undefined : clientNow + offsetMs;
}

function ttlRemaining(expiresAt: number, now: number): string {
  const total = Math.max(0, Math.ceil(expiresAt - now / 1_000));
  const days = Math.floor(total / 86_400);
  const hours = Math.floor(total % 86_400 / 3_600);
  const minutes = Math.floor(total % 3_600 / 60);
  const seconds = total % 60;
  const clock = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
  return days > 0 ? `${days}D ${clock}` : clock;
}

export function LockRow({ lock, index, now }: { lock: ActiveLock; index: number; now?: number }) {
  const expires = new Date(lock.expires_at * 1_000);
  return (
    <li className="reference-row lock-row">
      <span className="row-number">{String(index + 1).padStart(2, "0")}</span>
      <div className="reference-subject"><strong>{lock.repo_name}</strong><span>{lock.folder}</span></div>
      <dl className="reference-facts">
        <div><dt>HOLDER</dt><i aria-hidden="true" /><dd>{lock.holder_execution_id}</dd></div>
        <div><dt>TTL REMAINING</dt><i aria-hidden="true" /><dd>{now === undefined ? "SERVER CLOCK UNAVAILABLE" : <time dateTime={expires.toISOString()}>{ttlRemaining(lock.expires_at, now)}</time>}</dd></div>
        <div><dt>EXPIRES UTC</dt><i aria-hidden="true" /><dd><time dateTime={expires.toISOString()}>{utc.format(expires)} UTC</time></dd></div>
      </dl>
    </li>
  );
}

export function LocksPage() {
  const search = useSearch({ from: "/locks" });
  const navigate = useNavigate({ from: "/locks" });
  const query = useQuery({ queryKey: ["locks", search.cursor], queryFn: () => listLocks(search.cursor) });
  const now = useClock(responseClockOffset(query.data));
  const activeLocks = query.data?.locks.filter((lock) => now === undefined || lock.expires_at > now / 1_000) ?? [];
  return (
    <main className="procedure-page reference-page">
      <PageHeading code="LIVE CONCURRENCY REFERENCE" title="ACTIVE LOCKS" />
      <ReferenceState
        query={query}
        hasItems={() => activeLocks.length > 0}
        partialEmpty={Boolean(search.cursor || query.data?.cursor)}
        emptyTitle="NO ACTIVE LOCKS FILED"
        emptyNote="THE SERVER REPORTED NO ACTIVE LOCKS AT ITS OBSERVATION TIME."
      >
        {() => <ol className="reference-list">{activeLocks.map((lock, index) => <LockRow key={`${lock.repo_name}:${lock.folder}`} lock={lock} index={index} now={now} />)}</ol>}
      </ReferenceState>
      {!query.isPending && !query.isError && (
        <Pagination search={search} nextCursor={query.data.cursor} label="Active lock reference" navigate={(next) => void navigate({ search: next })} />
      )}
    </main>
  );
}

export function GateLedger({ gates }: { gates: GatesResponse }) {
  return (
    <div className="gate-ledger" aria-label="Read-only mutation gate state">
      <section className="memory-item gate-memory">
        <div><span>INSTALLATION CHALLENGE</span><strong>ENABLE_APPLY</strong></div>
        <BoolState value={gates.enable_apply} trueWord="ENABLED" falseWord="DISABLED" />
      </section>
      <div className="gate-section-heading">
        <strong>PER-FOLDER STATE OBSERVED AT LAST RUN</strong>
        <span>SOURCE: PINNED REPO CONFIG · NO MUTATION CONTROLS</span>
      </div>
      {gates.folders.length === 0 ? (
        <section className="empty-procedure">
          <strong>PER-FOLDER GATE STATE UNAVAILABLE</strong>
          <span>NO RETAINED LATEST-RUN OBSERVATIONS ARE FILED ON THIS BOUNDED PAGE. AN ABSENT ROW DOES NOT MEAN OPTED OUT.</span>
        </section>
      ) : gates.folders.map((folder) => (
        <section className="memory-item gate-memory folder-gate" key={`${folder.repo_name}:${folder.folder}`}>
          <div><span>{folder.repo_name}</span><strong>{folder.folder}</strong></div>
          <dl>
            <div><dt>APPLY</dt><dd><BoolState value={folder.apply} trueWord="OPTED IN" falseWord="NOT OPTED IN" /></dd></div>
            <div><dt>DESTROY</dt><dd><BoolState value={folder.destroy} trueWord="OPTED IN" falseWord="NOT OPTED IN" /></dd></div>
            <div><dt>SOURCE SHA</dt><dd>{folder.source_sha}</dd></div>
            <div><dt>OBSERVED AT</dt><dd><time dateTime={new Date(folder.observed_at * 1_000).toISOString()}>{utc.format(folder.observed_at * 1_000)} UTC</time></dd></div>
            <div><dt>OBSERVED BY</dt><dd>{folder.run_id} · {folder.trigger_id}</dd></div>
          </dl>
        </section>
      ))}
      <p className="fixed-measure">LATEST-RUN OBSERVATIONS ARE SHA-BOUND REFERENCE DATA. THEY MAY DIFFER BY FOLDER AND DO NOT CLAIM THE CURRENT BRANCH STATE. THIS PAGE CANNOT INITIATE OR ALTER A MUTATION.</p>
    </div>
  );
}

export function GatesPage() {
  const search = useSearch({ from: "/gates" });
  const navigate = useNavigate({ from: "/gates" });
  const query = useQuery({ queryKey: ["gates", search.cursor], queryFn: () => getGates(search.cursor) });
  return (
    <main className="procedure-page reference-page gates-page">
      <PageHeading code="READ-ONLY MEMORY ITEMS" title="GATES" />
      <ReferenceState
        query={query}
        hasItems={() => true}
        emptyTitle="GATE STATE UNAVAILABLE"
        emptyNote="INSTALLATION AND FOLDER GATE STATE HAS NOT BEEN REPORTED."
      >
        {(data) => <GateLedger gates={data} />}
      </ReferenceState>
      {!query.isPending && !query.isError && (
        <Pagination search={search} nextCursor={query.data.cursor} label="Gate observation reference" navigate={(next) => void navigate({ search: next })} />
      )}
    </main>
  );
}
