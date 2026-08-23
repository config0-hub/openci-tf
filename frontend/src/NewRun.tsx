// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { type FormEvent, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { createRun } from "./api";
import type { FolderMode, SafeRunAction } from "./types";

const safeActions: SafeRunAction[] = ["plan", "drift", "report"];
const pinnedSha = /^[0-9a-fA-F]{40}$/;

function foldersFrom(value: string): string[] {
  return [...new Set(value.split("\n").map((folder) => folder.trim()).filter(Boolean))];
}

export function NewRun() {
  const navigate = useNavigate({ from: "/runs/new" });
  const queryClient = useQueryClient();
  const [triggerId, setTriggerId] = useState("");
  const [commitHash, setCommitHash] = useState("");
  const [action, setAction] = useState<SafeRunAction>("plan");
  const [folderMode, setFolderMode] = useState<FolderMode>("all");
  const [folderText, setFolderText] = useState("");
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const folders = useMemo(() => foldersFrom(folderText), [folderText]);
  const checks = [
    { key: "trigger", valid: triggerId.trim().length > 0, text: "TRIGGER ID FILED" },
    { key: "sha", valid: pinnedSha.test(commitHash.trim()), text: "PINNED SHA · EXACTLY 40 HEX CHARACTERS" },
    { key: "verb", valid: safeActions.includes(action), text: "SAFE VERB · PLAN / DRIFT / REPORT" },
    { key: "folders", valid: folderMode === "all" || folders.length > 0, text: folderMode === "all" ? "FOLDER SCOPE · ALL" : "FOLDER SCOPE · EXPLICIT LIST FILED" },
    { key: "idempotency", valid: idempotencyKey.trim().length >= 8, text: "IDEMPOTENCY KEY · 8 OR MORE CHARACTERS" },
  ];
  const ready = checks.every((check) => check.valid);

  const mutation = useMutation({
    mutationFn: createRun,
    onSuccess: async ({ run_id }) => {
      await queryClient.invalidateQueries({ queryKey: ["runs"] });
      queryClient.removeQueries({ queryKey: ["run-detail", run_id] });
      await navigate({ to: "/runs/$runId", params: { runId: run_id } });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitted(true);
    mutation.reset();
    if (!ready) return;
    mutation.mutate({
      trigger_id: triggerId.trim(),
      commit_hash: commitHash.trim().toLocaleLowerCase(),
      action,
      folder_mode: folderMode,
      folders: folderMode === "explicit" ? folders : [],
      idempotency_key: idempotencyKey.trim(),
      notification_target: { type: "registry" },
    });
  }

  return (
    <main className="procedure-page new-run-page">
      <Link to="/" className="back-link">← RUN PROCEDURES</Link>
      <header className="page-heading">
        <div>
          <div className="procedure-code">NORMAL PROCEDURE · SAFE RUN INGRESS</div>
          <h1>NEW RUN PROCEDURE</h1>
        </div>
        <div className="retention-stamp">SAFE VERBS<br />ONLY</div>
      </header>

      <form
        className="procedure-form"
        onSubmit={submit}
        noValidate
        aria-label="File a safe run procedure"
        aria-describedby={submitted && !ready
          ? "procedure-validation-error"
          : mutation.isError ? "procedure-http-error" : undefined}
      >
        <div className="form-instruction memory-item">
          <strong>CHALLENGE · FILE A PINNED PROCEDURE</strong>
          <span>RESPONSE · API RETURNS 202 + RUN ID</span>
        </div>

        <section className="form-section" aria-labelledby="run-identity-heading">
          <h2 id="run-identity-heading">RUN IDENTITY</h2>
          <div className="field-grid">
            <label>
              <span>TRIGGER ID</span>
              <input
                name="trigger_id"
                value={triggerId}
                onChange={(event) => setTriggerId(event.target.value)}
                placeholder="payments-prod"
                required
                aria-invalid={submitted && !checks[0].valid || undefined}
                aria-describedby={submitted && !checks[0].valid ? "procedure-validation-error" : undefined}
              />
            </label>
            <label>
              <span>PINNED 40-CHAR SHA</span>
              <input
                name="commit_hash"
                value={commitHash}
                onChange={(event) => setCommitHash(event.target.value)}
                placeholder="4e8a4fb977e7fdf2bc891a1e21470c52aa44b634"
                inputMode="text"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                minLength={40}
                maxLength={40}
                required
                aria-invalid={submitted && !checks[1].valid || undefined}
                aria-describedby={submitted && !checks[1].valid ? "procedure-validation-error" : undefined}
              />
            </label>
          </div>
        </section>

        <section className="form-section" aria-labelledby="run-command-heading">
          <h2 id="run-command-heading">PROCEDURE COMMAND</h2>
          <fieldset className="procedure-fieldset">
            <legend>VERB</legend>
            <div className="choice-bank safe-verb-bank">
              {safeActions.map((safeAction) => (
                <label key={safeAction}>
                  <input
                    type="radio"
                    name="action"
                    value={safeAction}
                    checked={action === safeAction}
                    onChange={() => setAction(safeAction)}
                  />
                  <span><b aria-hidden="true">{action === safeAction ? "✓" : ""}</b>{safeAction.toLocaleUpperCase()}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="procedure-fieldset">
            <legend>FOLDER MODE</legend>
            <div className="choice-bank">
              {(["all", "explicit"] as FolderMode[]).map((mode) => (
                <label key={mode}>
                  <input
                    type="radio"
                    name="folder_mode"
                    value={mode}
                    checked={folderMode === mode}
                    onChange={() => setFolderMode(mode)}
                  />
                  <span><b aria-hidden="true">{folderMode === mode ? "✓" : ""}</b>{mode.toLocaleUpperCase()}</span>
                </label>
              ))}
            </div>
          </fieldset>

          {folderMode === "explicit" && (
            <label className="folder-list-field">
              <span>EXPLICIT FOLDERS · ONE PATH PER LINE</span>
              <textarea
                name="folders"
                value={folderText}
                onChange={(event) => setFolderText(event.target.value)}
                placeholder={"infra/api\ninfra/database"}
                rows={5}
                required
                spellCheck={false}
                aria-invalid={submitted && !checks[3].valid || undefined}
                aria-describedby={submitted && !checks[3].valid ? "procedure-validation-error" : undefined}
              />
            </label>
          )}
        </section>

        <section className="form-section" aria-labelledby="run-filing-heading">
          <h2 id="run-filing-heading">FILING CONTROL</h2>
          <label className="idempotency-field">
            <span>IDEMPOTENCY KEY</span>
            <input
              name="idempotency_key"
              value={idempotencyKey}
              onChange={(event) => setIdempotencyKey(event.target.value)}
              placeholder="console-run-482"
              minLength={8}
              required
              autoComplete="off"
              aria-invalid={submitted && !checks[4].valid || undefined}
              aria-describedby={submitted && !checks[4].valid ? "procedure-validation-error" : undefined}
            />
          </label>
          <div className="registry-target"><span>NOTIFICATION TARGET</span><strong>REGISTRY</strong></div>
        </section>

        <section className="validation-checklist" aria-label="Procedure validation checklist" aria-live="polite">
          <h2>PRE-FILE CHECKLIST</h2>
          <ol>
            {checks.map((check) => (
              <li key={check.key} data-valid={check.valid || undefined}>
                <span aria-hidden="true">{check.valid ? "✓" : "□"}</span>
                <strong>{check.text}</strong>
                <i key={`${check.valid}-leader`} aria-hidden="true" />
                <b key={String(check.valid)}>{check.valid ? "CHECK" : "OPEN"}</b>
              </li>
            ))}
          </ol>
        </section>

        {submitted && !ready && (
          <section id="procedure-validation-error" className="abnormal-block form-abnormal" role="alert">
            <div className="procedure-code">NON-NORMAL PROCEDURE · CHECKLIST OPEN</div>
            <h2>PROCEDURE NOT FILED</h2>
            <p>COMPLETE EVERY OPEN PRE-FILE ITEM, THEN FILE THE PROCEDURE AGAIN.</p>
          </section>
        )}

        {mutation.isError && (
          <section id="procedure-http-error" className="abnormal-block form-abnormal" role="alert">
            <div className="procedure-code">ABNORMAL PROCEDURE · HTTP FAILURE</div>
            <h2>RUN PROCEDURE NOT ACCEPTED</h2>
            <pre>{mutation.error.message}</pre>
            <p>VERIFY THE FILED VALUES, THEN RETRY THE SAME IDEMPOTENCY KEY.</p>
          </section>
        )}

        <div className="form-submit-row">
          <span>{ready ? "✓ CHECKLIST COMPLETE" : "□ CHECKLIST OPEN"}</span>
          <button type="submit" disabled={mutation.isPending}>
            {mutation.isPending ? "FILING PROCEDURE ▌" : mutation.isError ? "RETRY PROCEDURE" : "FILE PROCEDURE"}
          </button>
        </div>
      </form>
    </main>
  );
}
