export type RunState = "complete" | "in-progress" | "failed" | "drift";
export type SafeRunAction = "plan" | "drift" | "report";
export type FolderMode = "all" | "explicit";

export interface CreateRunRequest {
  trigger_id: string;
  commit_hash: string;
  action: SafeRunAction;
  folder_mode: FolderMode;
  folders: string[];
  idempotency_key: string;
  notification_target: { type: "registry" };
}

export interface CreateRunResponse {
  run_id: string;
}

export interface RunRecord {
  run_id: string;
  trigger_id: string;
  repo_name: string;
  commit_hash: string;
  action: SafeRunAction;
  status: string;
  drift_detected?: boolean;
  folder_count?: number;
  created_at: number;
  updated_at: number;
  expire_ttl: number;
}

export interface FolderRecord {
  run_id: string;
  folder: string;
  folder_id: string;
  account_id: string;
  execution_id: string;
  attempt: number;
  status: string;
  drift_detected?: boolean;
  updated_at: number;
  expire_ttl: number;
  manifest_s3_uri?: string;
  manifest_sha256?: string;
  outcome?: {
    succeeded?: boolean;
    error?: string;
    reply?: string;
  };
}

export interface ArtifactEntry {
  name: string;
  s3_uri: string;
  content_type: string;
  size: number;
  checksum: string;
  expires_at: string;
}

export interface Manifest {
  execution_id: string;
  action: string;
  generated_at: string;
  manifest_s3_uri: string;
  manifest_sha256: string;
  entries: ArtifactEntry[];
  failure_reason?: string;
}

export interface RunsResponse {
  runs: RunRecord[];
  cursor?: string;
}

export interface RunRegistryDetailData {
  run: RunRecord;
  folders: FolderRecord[];
  clockOffsetMs?: number;
}

export function runState(run: Pick<RunRecord, "status" | "drift_detected">): RunState {
  const status = run.status.toLocaleLowerCase();
  if (["failed", "infrastructure_error"].includes(status)) return "failed";
  if (run.drift_detected === true) return "drift";
  if (["accepted", "running", "in_progress"].includes(status)) return "in-progress";
  return "complete";
}

export function driftResultNote(
  run: Pick<RunRecord, "action" | "status" | "drift_detected">,
): string | undefined {
  if (run.action !== "drift" || runState(run) === "failed" || runState(run) === "in-progress") return undefined;
  if (run.drift_detected === false) return "DRIFT RESULT · CLEAN";
  if (run.drift_detected === undefined) return "DRIFT RESULT UNKNOWN · NO AUTHORITATIVE RESULT FILED";
  return undefined;
}

export const stateWords: Record<RunState, string> = {
  complete: "COMPLETE",
  "in-progress": "IN PROG",
  failed: "FAILED",
  drift: "DRIFT",
};

export const stateMarks: Record<RunState, string> = {
  complete: "✓",
  "in-progress": "▸",
  failed: "×",
  drift: "⚑",
};
