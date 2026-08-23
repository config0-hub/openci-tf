import { type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { listRuns, serverNow } from "./api";
import { advanceCursor, CursorPagination, retreatCursor, type CursorSearch } from "./Pagination";
import type { RunRecord } from "./types";
import { driftResultNote, runState, stateMarks, stateWords } from "./types";

export interface RunsSearch extends CursorSearch {
  trigger_id?: string;
  repo?: string;
}

const utc = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
  hour12: false,
});

export function RunStateEvidence({ run }: { run: Pick<RunRecord, "action" | "status" | "drift_detected"> }) {
  const state = runState(run);
  const note = driftResultNote(run);
  return (
    <span className="state-evidence">
      <span className="state-word" aria-label={`Run state: ${stateWords[state]}`}>
        <b aria-hidden="true">{stateMarks[state]}</b>{stateWords[state]}
      </span>
      {note && <em className="drift-result-note">{note}</em>}
    </span>
  );
}

export function RunsIndex() {
  const search = useSearch({ from: "/" });
  const navigate = useNavigate({ from: "/" });
  const triggerId = search.trigger_id ?? "";
  const repo = search.repo ?? "";
  const runsQuery = useQuery({
    queryKey: ["runs", triggerId, repo, search.cursor],
    queryFn: () => listRuns(triggerId, repo, search.cursor),
    refetchInterval: (query) => query.state.data?.runs.some((run) => runState(run) === "in-progress") ? 5_000 : false,
  });

  function filter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const nextTrigger = String(form.get("trigger_id") ?? "").trim() || undefined;
    const nextRepo = String(form.get("repo") ?? "").trim() || undefined;
    void navigate({ search: { trigger_id: nextTrigger, repo: nextRepo } });
  }

  const observedNow = serverNow(runsQuery.data);
  const runs = (runsQuery.data?.runs ?? []).filter((run) =>
    observedNow === undefined || run.expire_ttl > observedNow / 1_000,
  );
  const partialEmpty = Boolean(search.cursor || runsQuery.data?.cursor);

  function nextPage() {
    const cursor = runsQuery.data?.cursor;
    if (!cursor) return;
    const next = advanceCursor(search, cursor);
    void navigate({ search: { ...search, ...next } });
  }

  function previousPage() {
    const previous = retreatCursor(search);
    void navigate({ search: { ...search, ...previous } });
  }

  return (
    <main className="procedure-page runs-page">
      <header className="page-heading">
        <div>
          <div className="procedure-code">NORMAL / NON-NORMAL PROCEDURES</div>
          <h1>RUN PROCEDURES</h1>
        </div>
        <div className="retention-stamp">REGISTRY<br />TIME-LIMITED</div>
      </header>

      <div className="memory-item new-procedure" aria-label="New procedure entry point">
        <strong>NEW PROCEDURE</strong>
        <Link to="/runs/new" className="new-procedure-action">FILE SAFE RUN →</Link>
      </div>

      <form
        className="filter-strip"
        role="search"
        aria-label="Filter run procedures"
        onSubmit={filter}
        key={`${triggerId}:${repo}`}
      >
        <label>
          TRIGGER ID · OPTIONAL
          <input name="trigger_id" defaultValue={triggerId} placeholder="all authorized triggers" />
        </label>
        <label>
          REPO CONTAINS · SERVER FILTER
          <input name="repo" defaultValue={repo} placeholder="all authorized repos" />
        </label>
        <button type="submit">FILE FILTER</button>
      </form>

      {runsQuery.isPending ? (
        <div className="run-list loading-list" role="status" aria-label="Loading run procedures" aria-live="polite">
          {[1, 2, 3, 4].map((row) => (
            <div className="loading-row" key={row}>
              <span>{String(row).padStart(2, "0")}</span><i /><span>DRAWING</span>
            </div>
          ))}
        </div>
      ) : runsQuery.isError ? (
        <section className="abnormal-block" role="alert">
          <div className="procedure-code">ABNORMAL PROCEDURE · HTTP FAILURE</div>
          <h2>RUN REGISTRY UNAVAILABLE</h2>
          <pre>{runsQuery.error.message}</pre>
          <button type="button" onClick={() => void runsQuery.refetch()}>RETRY REGISTRY ITEM</button>
        </section>
      ) : runs.length === 0 ? (
        <section className="empty-procedure">
          <strong>{partialEmpty
            ? "NO PROCEDURES FILED ON THIS BOUNDED PAGE"
            : "NO PROCEDURES FILED — RUNS APPEAR WHEN A PR COMMENT OR API CALL CREATES ONE"}</strong>
          <span>{partialEmpty
            ? "THIS CURSORED SLICE IS EMPTY. REVIEW THE ADJACENT BOUNDED PAGES BEFORE CONCLUDING THE REGISTRY IS EMPTY."
            : "THE SERVER APPLIED THE AUTHORIZED TRIGGER AND REPOSITORY SCOPE."}</span>
        </section>
      ) : (
        <ol className="run-list">
          {runs.map((run, index) => {
            const state = runState(run);
            return (
              <li key={run.run_id} className="run-row" data-state={state}>
                <Link to="/runs/$runId" params={{ runId: run.run_id }}>
                  <span className="row-number">{String(index + 1).padStart(2, "0")}</span>
                  <span className="run-subject">
                    <strong>{run.repo_name}</strong>
                    <small>{run.folder_count ?? "—"} FOLDERS · {run.action.toLocaleUpperCase()}</small>
                  </span>
                  <span key={`leader-${state}`} className="leader" aria-hidden="true" />
                  <RunStateEvidence run={run} />
                  <span className="run-retention">
                    FILED {utc.format(run.created_at * 1_000)} · RETAINED TO {utc.format(run.expire_ttl * 1_000)} UTC
                  </span>
                </Link>
              </li>
            );
          })}
        </ol>
      )}

      {!runsQuery.isPending && !runsQuery.isError && (
        <CursorPagination
          page={(search.history?.length ?? 0) + 1}
          hasNext={Boolean(runsQuery.data.cursor)}
          hasPrevious={Boolean(search.history?.length)}
          label="Run registry"
          onNext={nextPage}
          onPrevious={previousPage}
        />
      )}
    </main>
  );
}
