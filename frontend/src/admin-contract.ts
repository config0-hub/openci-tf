// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
// Read-only admin API contract exposed by the core API.
export const adminEndpoints = {
  repos: "/repos",
  accounts: "/accounts",
  locks: "/locks",
  gates: "/gates",
} as const;

export interface RepoRegistration {
  repo_name: string;
  trigger_ids: string[];
  require_approval: boolean;
}

export interface ReposResponse {
  repos: RepoRegistration[];
  cursor?: string;
}

export interface AccountTarget {
  alias: string;
  account_id: string;
  role_name: string;
}

export interface AccountsResponse {
  accounts: AccountTarget[];
  cursor?: string;
}

export interface ActiveLock {
  repo_name: string;
  folder: string;
  holder_execution_id: string;
  expires_at: number;
}

export interface LocksResponse {
  locks: ActiveLock[];
  cursor?: string;
}

export interface FolderGate {
  repo_name: string;
  folder: string;
  trigger_id: string;
  run_id: string;
  source_sha: string;
  apply: boolean;
  destroy: boolean;
  observed_at: number;
}

export interface GatesResponse {
  /** Install-level flag is retired; apply enablement is per-account
   *  (see enable_apply_scope). This stays false until the API exposes
   *  per-account enablement. */
  enable_apply: boolean;
  enable_apply_scope?: string;
  folders: FolderGate[];
  folders_source: "latest-run-observation";
  cursor?: string;
}
