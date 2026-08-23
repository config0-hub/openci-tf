import { Fragment, useEffect, useState } from "react";
import { queryOptions, useQueries, useQuery } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { getRunManifest, getRunRegistryDetail } from "./api";
import type { ArtifactEntry, FolderRecord, Manifest, RunState } from "./types";
import { driftResultNote, runState, stateMarks, stateWords } from "./types";

const dateTime = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  timeZone: "UTC",
  hour12: false,
});
const dateOnly = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit", month: "short", year: "numeric", timeZone: "UTC",
});

function folderState(record: FolderRecord): RunState {
  if (["failed", "infrastructure_error"].includes(record.status)) return "failed";
  if (record.drift_detected === true) return "drift";
  if (["accepted", "running", "in_progress"].includes(record.status)) return "in-progress";
  return "complete";
}

export function runRegistryDetailQueryOptions(runId: string) {
  return queryOptions({
    queryKey: ["run-detail-registry", runId] as const,
    queryFn: () => getRunRegistryDetail(runId),
    refetchInterval: (query) => query.state.data && runState(query.state.data.run) === "in-progress" ? 5_000 : false,
  });
}

export function runManifestQueryOptions(runId: string, record: FolderRecord) {
  const digest = record.manifest_sha256 || record.manifest_s3_uri;
  if (!digest) throw new Error("manifest query requires a checksum or S3 URI");
  return queryOptions({
    queryKey: ["run-manifest", runId, record.folder_id, digest] as const,
    queryFn: () => getRunManifest(runId, record.folder_id),
    staleTime: Infinity,
  });
}

function useServerClock(offsetMs: number | undefined): number | undefined {
  const [clientNow, setClientNow] = useState(Date.now);
  useEffect(() => {
    const interval = window.setInterval(() => setClientNow(Date.now()), 1_000);
    return () => window.clearInterval(interval);
  }, []);
  return offsetMs === undefined ? undefined : clientNow + offsetMs;
}

function bytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${(value / 1_024).toFixed(1)} KB`;
  return `${(value / 1_048_576).toFixed(1)} MB`;
}

function stepOutcome(manifest: Manifest | undefined, name: string) {
  if (manifest?.entries.some((entry) => entry.name === name)) return { mark: "■", word: "FILED" };
  return { mark: "·", word: "NOT FILED" };
}

function ArtifactItem({ artifact, generatedAt, observedNow }: { artifact: ArtifactEntry; generatedAt: string; observedNow?: number }) {
  const isPlan = artifact.name === "plan.tfplan";
  const expired = observedNow === undefined ? undefined : Date.parse(artifact.expires_at) <= observedNow;
  return (
    <li className="timeline-item artifact-item" data-expired={isPlan && expired || undefined}>
      <time>{dateTime.format(Date.parse(generatedAt))}</time>
      <div>
        <strong>ARTIFACT · {artifact.name}</strong>
        {isPlan && expired === true ? (
          <p className="expired-plan">PLAN EXPIRED {dateOnly.format(Date.parse(artifact.expires_at))} — RE-RUN PLAN.</p>
        ) : isPlan ? (
          <p className="s3-pointer">
            PLAN POINTER · {artifact.s3_uri}<br />
            EXPIRES {dateTime.format(Date.parse(artifact.expires_at))} UTC
            {expired === undefined && <><br />EXPIRY DECISION UNAVAILABLE · SERVER CLOCK NOT REPORTED</>}
          </p>
        ) : null}
        <dl className="artifact-meta">
          <div><dt>SIZE</dt><dd>{bytes(artifact.size)}</dd></div>
          <div><dt>SHA256</dt><dd>{artifact.checksum}</dd></div>
          <div><dt>EXPIRY</dt><dd>{dateTime.format(Date.parse(artifact.expires_at))} UTC</dd></div>
        </dl>
      </div>
    </li>
  );
}

export function RunDetail() {
  const { runId } = useParams({ from: "/runs/$runId" });
  const detailQuery = useQuery(runRegistryDetailQueryOptions(runId));
  const manifestRecords = detailQuery.data?.folders.filter((record) => record.manifest_s3_uri) ?? [];
  const manifestQueries = useQueries({
    queries: manifestRecords.map((record) => runManifestQueryOptions(runId, record)),
  });
  const observedNow = useServerClock(detailQuery.data?.clockOffsetMs);
  const manifestError = manifestQueries.find((query) => query.isError);

  if (detailQuery.isPending || manifestQueries.some((query) => query.isPending)) {
    return (
      <main className="procedure-page detail-page">
        <div className="detail-loading" role="status" aria-live="polite"><span aria-hidden="true" />DRAWING EXECUTION TIME AXIS</div>
      </main>
    );
  }

  if (detailQuery.isError || manifestError) {
    const error = detailQuery.error ?? manifestError?.error;
    return (
      <main className="procedure-page detail-page">
        <section className="abnormal-block" role="alert">
          <div className="procedure-code">ABNORMAL PROCEDURE · HTTP FAILURE</div>
          <h1>RUN RECORD UNAVAILABLE</h1>
          <pre>{error?.message}</pre>
          <button type="button" onClick={() => {
            void detailQuery.refetch();
            for (const query of manifestQueries) {
              if (query.isError) void query.refetch();
            }
          }}>RETRY RUN ITEM</button>
        </section>
      </main>
    );
  }

  const { run } = detailQuery.data;
  const manifestByFolder = new Map(
    manifestRecords.map((record, index) => [record.folder_id, manifestQueries[index].data]),
  );
  const folders = detailQuery.data.folders.map((record) => ({
    record,
    manifest: manifestByFolder.get(record.folder_id),
  }));
  const state = runState(run);
  const runDriftNote = driftResultNote(run);
  return (
    <main className="procedure-page detail-page">
      <Link to="/" className="back-link">← RUN PROCEDURES</Link>
      <header className="page-heading detail-heading">
        <div>
          <div className="procedure-code">RUN · {run.run_id}</div>
          <h1>{run.repo_name}</h1>
        </div>
        <span key={`verdict-leader-${state}`} className="verdict-leader" aria-hidden="true" />
        <div
          key={`verdict-${state}`}
          className="verdict-stamp"
          data-state={state}
          role="status"
          aria-label={`Run state: ${stateWords[state]}`}
        >
          <b aria-hidden="true">{stateMarks[state]}</b>{stateWords[state]}
        </div>
      </header>

      <dl className="run-facts">
        <div><dt>TRIGGER</dt><dd>{run.trigger_id}</dd></div>
        <div><dt>VERB</dt><dd>{run.action.toLocaleUpperCase()}</dd></div>
        <div><dt>PINNED SHA</dt><dd>{run.commit_hash}</dd></div>
        <div><dt>REGISTRY EXPIRY</dt><dd>{dateTime.format(run.expire_ttl * 1_000)} UTC</dd></div>
      </dl>
      {runDriftNote && <p className="run-observation-note">{runDriftNote}</p>}

      <ol className="execution-timeline" aria-label="Folder execution timeline">
        <li className="timeline-item run-origin">
          <time>{dateTime.format(run.created_at * 1_000)}</time>
          <div><strong>RUN ACCEPTED</strong><span>PROCEDURE FILED</span></div>
        </li>
        {folders.map(({ record, manifest }, folderIndex) => {
          const currentState = folderState(record);
          const corrective = manifest?.failure_reason ?? record.outcome?.error;
          const folderDriftNote = run.action === "drift"
            ? driftResultNote({ action: "drift", status: record.status, drift_detected: record.drift_detected })
            : undefined;
          const planStep = run.action === "drift" ? "drift.json" : "tf/plan.out";
          const steps = [["INIT", "init.out"], ["VALIDATE", "validate.out"], [run.action === "drift" ? "DRIFT" : "PLAN", planStep]];
          return (
            <Fragment key={record.execution_id}>
              <li className="timeline-item folder-item" data-state={currentState}>
                <time>{dateTime.format(record.updated_at * 1_000)}</time>
                <div>
                  <span className="subprocedure-number">{String(folderIndex + 1).padStart(2, "0")}</span>
                  <strong>{record.folder}</strong>
                  <span>ACCOUNT {record.account_id} · ATTEMPT {record.attempt + 1} · STATE {stateWords[currentState]}</span>
                  {folderDriftNote && <span className="folder-observation-note">{folderDriftNote}</span>}
                </div>
              </li>
              {steps.map(([label, artifactName]) => {
                const outcome = stepOutcome(manifest, artifactName);
                return (
                  <li className="timeline-item step-item" key={`${record.execution_id}-${label}`}>
                    <time>—</time>
                    <div>
                      <strong>{label}</strong>
                      <i key={`${outcome.word}-leader`} aria-hidden="true" />
                      <span key={outcome.word} className="step-outcome" aria-label={`${label} state: ${outcome.word}`}>
                        <b aria-hidden="true">{outcome.mark}</b>{outcome.word}
                      </span>
                    </div>
                  </li>
                );
              })}
              {corrective && (
                <li className="timeline-item corrective-item">
                  <time>{dateTime.format(record.updated_at * 1_000)}</time>
                  <div><strong>CORRECTIVE EVIDENCE</strong><pre>{corrective}</pre></div>
                </li>
              )}
              {manifest?.entries.map((artifact) => (
                <ArtifactItem key={`${record.execution_id}-${artifact.name}`} artifact={artifact} generatedAt={manifest.generated_at} observedNow={observedNow} />
              ))}
            </Fragment>
          );
        })}
        <li className="timeline-item run-terminal">
          <time>{dateTime.format(run.updated_at * 1_000)}</time>
          <div>
            <strong>RUN {stateWords[state]}</strong>
            <span>{stateMarks[state]} {state === "in-progress" ? "LATEST REGISTRY STATE" : "TERMINAL REGISTRY STATE"}</span>
          </div>
        </li>
      </ol>
    </main>
  );
}
