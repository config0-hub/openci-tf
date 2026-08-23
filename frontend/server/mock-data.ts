// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
const HOUR = 3_600;
const DAY = 86_400;
const now = Math.floor(Date.now() / 1_000);
const sha = "4e8a4fb977e7fdf2bc891a1e21470c52aa44b634";
const digest = "71f75b8166571193a881b99b1a6e71f9f459f551943492e225c584fdc550dc8";

export const mockRepos = [
  { repo_name: "acme/payments-infra", trigger_ids: ["payments-prod", "payments-staging"], require_approval: true },
  { repo_name: "acme/network-foundation", trigger_ids: ["network-prod"], require_approval: false },
  { repo_name: "acme/edge-routing", trigger_ids: ["edge-prod"], require_approval: true },
];
export const emptyMockRepos: typeof mockRepos = [];

export const mockAccounts = [
  {
    alias: "hub-production",
    account_id: "123456789012",
    role_name: "openci-tf-executor-local",
  },
  {
    alias: "network-production",
    account_id: "210987654321",
    role_name: "openci-tf-executor-remote",
  },
  {
    alias: "edge-production",
    account_id: "321098765432",
    role_name: "openci-tf-executor-edge",
  },
];
export const emptyMockAccounts: typeof mockAccounts = [];

export const mockLocks = [
  {
    repo_name: "acme/payments-infra",
    folder: "infra/database",
    holder_execution_id: "payments-prod-pr482-c9918.database.0",
    expires_at: now + 46 * 60,
  },
  {
    repo_name: "acme/network-foundation",
    folder: "infra/network",
    holder_execution_id: "network-prod-api07c2.network.0",
    expires_at: now + 2 * HOUR + 17 * 60,
  },
  {
    repo_name: "acme/expired-fixture",
    folder: "infra/old",
    holder_execution_id: "expired-lock-fixture",
    expires_at: now - 30,
  },
];
export const emptyMockLocks: typeof mockLocks = [];

export const mockGates = {
  enable_apply: true,
  folders_source: "latest-run-observation" as const,
  folders: [
    {
      repo_name: "acme/payments-infra",
      folder: "infra/api",
      trigger_id: "payments-prod",
      run_id: "run-complete",
      source_sha: sha,
      apply: true,
      destroy: false,
      observed_at: now - 7_420,
    },
    {
      repo_name: "acme/payments-infra",
      folder: "infra/database",
      trigger_id: "payments-prod",
      run_id: "run-failed",
      source_sha: sha,
      apply: true,
      destroy: true,
      observed_at: now - 14_200,
    },
    {
      repo_name: "acme/network-foundation",
      folder: "infra/network",
      trigger_id: "network-prod",
      run_id: "run-complete",
      source_sha: sha,
      apply: false,
      destroy: false,
      observed_at: now - 7_420,
    },
  ],
};
export const emptyMockGates: typeof mockGates = {
  enable_apply: false,
  folders_source: "latest-run-observation",
  folders: [],
};

export const mockRuns = [
  {
    run_id: "run-progress",
    trigger_id: "payments-prod",
    repo_name: "acme/payments-infra",
    commit_hash: sha,
    action: "plan",
    status: "running",
    folder_count: 3,
    created_at: now - 420,
    updated_at: now - 70,
    expire_ttl: now + 89 * DAY,
  },
  {
    run_id: "run-complete",
    trigger_id: "payments-prod",
    repo_name: "acme/network-foundation",
    commit_hash: sha,
    action: "report",
    status: "succeeded",
    folder_count: 2,
    created_at: now - 7_420,
    updated_at: now - 6_900,
    expire_ttl: now + 88 * DAY,
  },
  {
    run_id: "run-failed",
    trigger_id: "payments-prod",
    repo_name: "acme/payments-infra",
    commit_hash: sha,
    action: "plan",
    status: "failed",
    folder_count: 2,
    created_at: now - 14_200,
    updated_at: now - 13_780,
    expire_ttl: now + 87 * DAY,
  },
  {
    run_id: "run-drift",
    trigger_id: "payments-prod",
    repo_name: "acme/edge-routing",
    commit_hash: sha,
    action: "drift",
    status: "succeeded",
    drift_detected: true,
    folder_count: 1,
    created_at: now - DAY,
    updated_at: now - DAY + 330,
    expire_ttl: now + 86 * DAY,
  },
  {
    run_id: "run-drift-clean",
    trigger_id: "network-prod",
    repo_name: "acme/network-foundation",
    commit_hash: sha,
    action: "drift",
    status: "succeeded",
    drift_detected: false,
    folder_count: 1,
    created_at: now - DAY - 900,
    updated_at: now - DAY - 620,
    expire_ttl: now + 86 * DAY,
  },
  {
    run_id: "run-drift-unknown",
    trigger_id: "edge-prod",
    repo_name: "acme/edge-routing",
    commit_hash: sha,
    action: "drift",
    status: "succeeded",
    folder_count: 1,
    created_at: now - DAY - 1_800,
    updated_at: now - DAY - 1_510,
    expire_ttl: now + 86 * DAY,
  },
  {
    run_id: "run-expired-plan",
    trigger_id: "payments-prod",
    repo_name: "acme/payments-infra",
    commit_hash: sha,
    action: "plan",
    status: "succeeded",
    folder_count: 1,
    created_at: now - 3 * DAY,
    updated_at: now - 3 * DAY + 410,
    expire_ttl: now + 84 * DAY,
  },
];

interface MockFolder {
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
  outcome?: { succeeded: boolean; error: string };
}

const folder = (run_id: string, path: string, status: string, offset: number, manifest = true): MockFolder => ({
  run_id,
  folder: path,
  folder_id: Buffer.from(path).toString("base64url"),
  account_id: path.includes("network") ? "210987654321" : "123456789012",
  execution_id: `${run_id}.${offset}.0`,
  attempt: 0,
  status,
  updated_at: now + offset,
  expire_ttl: now + 84 * DAY,
  ...(manifest ? {
    manifest_s3_uri: `s3://openci-tf-tmp-123456789012/openci-tf/acme/${run_id}/${path}/manifest.json`,
    manifest_sha256: digest,
  } : {}),
});

export const mockFolders: Record<string, ReturnType<typeof folder>[]> = {
  "run-progress": [folder("run-progress", "infra/api", "running", -70, false)],
  "run-complete": [
    folder("run-complete", "infra/network", "succeeded", -6_960),
    folder("run-complete", "infra/dns", "succeeded", -6_900),
  ],
  "run-failed": [
    folder("run-failed", "infra/api", "succeeded", -13_850),
    { ...folder("run-failed", "infra/database", "failed", -13_780), outcome: { succeeded: false, error: "terraform validate exited 1: unsupported argument at database/main.tf:42" } },
  ],
  "run-drift": [
    { ...folder("run-drift", "infra/edge", "succeeded", -DAY + 330), drift_detected: true },
  ],
  "run-drift-clean": [
    { ...folder("run-drift-clean", "infra/network", "succeeded", -DAY - 620), drift_detected: false },
  ],
  "run-drift-unknown": [folder("run-drift-unknown", "infra/edge", "succeeded", -DAY - 1_510)],
  "run-expired-plan": [folder("run-expired-plan", "infra/api", "succeeded", -3 * DAY + 410)],
};

const entry = (name: string, run: string, path: string, expires: number, size = 1_248) => ({
  name,
  s3_uri: `s3://openci-tf-tmp-123456789012/openci-tf/acme/${run}/${path}/${name}`,
  content_type: name.endsWith(".json") ? "application/json" : "text/plain",
  size,
  checksum: digest,
  expires_at: new Date(expires * 1_000).toISOString().replace(".000Z", "Z"),
});

function manifest(run: string, path: string, action: string, expires: number, failure_reason?: string) {
  const planName = action === "drift" ? "drift.json" : "tf/plan.out";
  const entries = [
    entry("init.out", run, path, expires, 928),
    entry("validate.out", run, path, expires, 314),
    entry(planName, run, path, expires, 5_842),
  ];
  if (action !== "drift" && !failure_reason) {
    entries.push({ ...entry("plan.tfplan", run, path, expires, 8_413_120), content_type: "application/octet-stream" });
    entries.push(entry("tfsec.json", run, path, expires, 2_048));
    entries.push(entry("infracost.json", run, path, expires, 1_416));
  }
  return {
    version: 1,
    run_id: run,
    repo_name: "acme/payments-infra",
    commit_hash: sha,
    account_id: "123456789012",
    folder: path,
    action,
    attempt: 0,
    execution_id: `${run}.0.0`,
    generated_at: new Date((mockRuns.find((item) => item.run_id === run)?.updated_at ?? now - HOUR) * 1_000).toISOString().replace(".000Z", "Z"),
    manifest_s3_uri: `s3://openci-tf-tmp-123456789012/openci-tf/acme/${run}/${path}/manifest.json`,
    manifest_sha256: digest,
    entries,
    ...(failure_reason ? { failure_reason } : {}),
  };
}

export const mockManifests: Record<string, ReturnType<typeof manifest>> = {
  "run-complete/infra/network": manifest("run-complete", "infra/network", "report", now + DAY),
  "run-complete/infra/dns": manifest("run-complete", "infra/dns", "report", now + DAY),
  "run-failed/infra/api": manifest("run-failed", "infra/api", "plan", now + DAY),
  "run-failed/infra/database": manifest("run-failed", "infra/database", "plan", now + DAY, "terraform validate exited 1: unsupported argument at database/main.tf:42"),
  "run-drift/infra/edge": manifest("run-drift", "infra/edge", "drift", now + DAY),
  "run-drift-clean/infra/network": manifest("run-drift-clean", "infra/network", "drift", now + DAY),
  "run-drift-unknown/infra/edge": manifest("run-drift-unknown", "infra/edge", "drift", now + DAY),
  "run-expired-plan/infra/api": manifest("run-expired-plan", "infra/api", "plan", now - 2 * DAY),
};
