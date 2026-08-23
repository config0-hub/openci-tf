// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { adminEndpoints, type AccountsResponse, type GatesResponse, type LocksResponse, type ReposResponse } from "./admin-contract";
import type { CreateRunRequest, CreateRunResponse, FolderRecord, Manifest, RunRecord, RunRegistryDetailData, RunsResponse } from "./types";

export const TOKEN_KEY = "openci-tf-console-token";
export const TOKEN_CHANGED = "openci-tf-console-token-changed";
export const TOKEN_REJECTED = "openci-tf-console-token-rejected";

const RESPONSE_CLOCK_OFFSET = "__openciTfClockOffsetMs" as const;
const PAGE_LIMIT = import.meta.env.VITE_CONSOLE_MOCK === "true" ? "2" : "25";

type TimedResponse<T> = T & { readonly [RESPONSE_CLOCK_OFFSET]?: number };

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function consoleToken(): string {
  const token = sessionStorage.getItem(TOKEN_KEY);
  if (!token) throw new ApiError(401, "Shared console bearer token is required.");
  return token;
}

export async function apiFetch<T>(path: string, init: RequestInit = {}, expectedStatus?: number): Promise<TimedResponse<T>> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${consoleToken()}`);
  if (init.body !== undefined && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api${path}`, { ...init, headers });
  const receivedAt = Date.now();
  const text = await response.text();
  if (!response.ok) {
    if (response.status === 401) {
      sessionStorage.removeItem(TOKEN_KEY);
      window.dispatchEvent(new Event(TOKEN_REJECTED));
      window.dispatchEvent(new Event(TOKEN_CHANGED));
    }
    throw new ApiError(response.status, text || response.statusText);
  }
  if (expectedStatus !== undefined && response.status !== expectedStatus) {
    throw new ApiError(response.status, `Expected HTTP ${expectedStatus}; received HTTP ${response.status}. ${text}`);
  }
  const parsed = JSON.parse(text) as TimedResponse<T>;
  const serverDate = response.headers.get("Date");
  const serverTime = serverDate ? Date.parse(serverDate) : Number.NaN;
  if (parsed && typeof parsed === "object" && Number.isFinite(serverTime)) {
    Object.defineProperty(parsed, RESPONSE_CLOCK_OFFSET, { value: serverTime - receivedAt, enumerable: true });
  }
  return parsed;
}

export function responseClockOffset(response: object | undefined): number | undefined {
  return response && RESPONSE_CLOCK_OFFSET in response
    ? (response as TimedResponse<object>)[RESPONSE_CLOCK_OFFSET]
    : undefined;
}

export function serverNow(response: object | undefined, clientNow = Date.now()): number | undefined {
  const offset = responseClockOffset(response);
  return offset === undefined ? undefined : clientNow + offset;
}

function pagedPath(path: string, cursor?: string): string {
  const search = new URLSearchParams({ limit: PAGE_LIMIT });
  if (cursor) search.set("cursor", cursor);
  return `${path}?${search}`;
}

export function listRuns(triggerId: string, repo: string, cursor?: string): Promise<RunsResponse> {
  const search = new URLSearchParams({ limit: PAGE_LIMIT });
  if (triggerId) search.set("trigger_id", triggerId);
  if (repo) search.set("repo", repo);
  if (cursor) search.set("cursor", cursor);
  return apiFetch<RunsResponse>(`/runs?${search}`);
}

export async function probeAuthorization(): Promise<true> {
  await apiFetch<RunsResponse>(`/runs?limit=1`);
  return true;
}

export function createRun(request: CreateRunRequest): Promise<CreateRunResponse> {
  return apiFetch<CreateRunResponse>("/runs", { method: "POST", body: JSON.stringify(request) }, 202);
}

export const listRepos = (cursor?: string): Promise<ReposResponse> => apiFetch<ReposResponse>(pagedPath(adminEndpoints.repos, cursor));
export const listAccounts = (cursor?: string): Promise<AccountsResponse> => apiFetch<AccountsResponse>(pagedPath(adminEndpoints.accounts, cursor));
export const listLocks = (cursor?: string): Promise<LocksResponse> => apiFetch<LocksResponse>(pagedPath(adminEndpoints.locks, cursor));
export const getGates = (cursor?: string): Promise<GatesResponse> => apiFetch<GatesResponse>(pagedPath(adminEndpoints.gates, cursor));

export async function getRunRegistryDetail(runId: string): Promise<RunRegistryDetailData> {
  const encodedRun = encodeURIComponent(runId);
  const run = await apiFetch<RunRecord>(`/runs/${encodedRun}`);
  const clockOffsetMs = responseClockOffset(run);
  const folderResponse = await apiFetch<{ folders: FolderRecord[] }>(`/runs/${encodedRun}/folders`);
  return { run, folders: folderResponse.folders, clockOffsetMs };
}

export const MANIFEST_FETCH_CONCURRENCY = 4;

let activeManifestFetches = 0;
const pendingManifestFetches: Array<() => void> = [];

function withManifestFetchSlot<T>(operation: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const start = () => {
      activeManifestFetches += 1;
      void operation().then(resolve, reject).finally(() => {
        activeManifestFetches -= 1;
        pendingManifestFetches.shift()?.();
      });
    };
    if (activeManifestFetches < MANIFEST_FETCH_CONCURRENCY) start();
    else pendingManifestFetches.push(start);
  });
}

export function getRunManifest(runId: string, folderId: string): Promise<Manifest> {
  return withManifestFetchSlot(() => apiFetch<Manifest>(
    `/runs/${encodeURIComponent(runId)}/folders/${encodeURIComponent(folderId)}/manifest`,
  ));
}
