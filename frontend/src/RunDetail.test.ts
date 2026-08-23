import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MANIFEST_FETCH_CONCURRENCY, getRunManifest } from "./api";
import { runManifestQueryOptions, runRegistryDetailQueryOptions } from "./RunDetail";
import type { FolderRecord, Manifest, RunRecord } from "./types";

const run: RunRecord = {
  run_id: "run-1",
  trigger_id: "trigger-1",
  repo_name: "acme/infrastructure",
  commit_hash: "a".repeat(40),
  action: "plan",
  status: "running",
  created_at: 1_700_000_000,
  updated_at: 1_700_000_001,
  expire_ttl: 4_102_444_800,
};

const folder: FolderRecord = {
  run_id: run.run_id,
  folder: "infra/network",
  folder_id: "infra-network",
  account_id: "123456789012",
  execution_id: "run-1.network.0",
  attempt: 0,
  status: "running",
  updated_at: 1_700_000_001,
  expire_ttl: 4_102_444_800,
  manifest_s3_uri: "s3://artifacts/run-1/infra/network/manifest.json",
  manifest_sha256: "b".repeat(64),
};

const manifest: Manifest = {
  execution_id: folder.execution_id,
  action: "plan",
  generated_at: "2025-01-01T00:00:00Z",
  manifest_s3_uri: folder.manifest_s3_uri!,
  manifest_sha256: folder.manifest_sha256!,
  entries: [],
};

function installToken(): void {
  vi.stubGlobal("sessionStorage", {
    getItem: () => "test-token",
    removeItem: () => undefined,
    setItem: () => undefined,
  });
}

function jsonResponse(body: object): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("run detail polling", () => {
  it("polls registry state without refetching an unchanged manifest", async () => {
    installToken();
    const requested: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      const path = String(input);
      requested.push(path);
      if (path.endsWith("/folders")) return jsonResponse({ folders: [folder] });
      if (path.endsWith("/manifest")) return jsonResponse(manifest);
      return jsonResponse(run);
    }));

    const client = new QueryClient();
    const registryOptions = runRegistryDetailQueryOptions(run.run_id);
    const firstRegistry = await client.fetchQuery(registryOptions);
    const manifestOptions = runManifestQueryOptions(run.run_id, firstRegistry.folders[0]);
    await client.fetchQuery(manifestOptions);

    await client.invalidateQueries({ queryKey: registryOptions.queryKey });
    const polledRegistry = await client.fetchQuery(registryOptions);
    await client.fetchQuery(runManifestQueryOptions(run.run_id, polledRegistry.folders[0]));

    expect(requested.filter((path) => path.endsWith(`/runs/${run.run_id}`))).toHaveLength(2);
    expect(requested.filter((path) => path.endsWith("/folders"))).toHaveLength(2);
    expect(requested.filter((path) => path.endsWith("/manifest"))).toHaveLength(1);
    expect(manifestOptions.queryKey).toEqual([
      "run-manifest",
      run.run_id,
      folder.folder_id,
      folder.manifest_sha256,
    ]);
    expect(manifestOptions.staleTime).toBe(Infinity);
    client.clear();
  });

  it("bounds concurrent manifest requests", async () => {
    installToken();
    let active = 0;
    let maximumActive = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      active += 1;
      maximumActive = Math.max(maximumActive, active);
      await new Promise((resolve) => setTimeout(resolve, 5));
      active -= 1;
      return jsonResponse(manifest);
    }));

    await Promise.all(
      Array.from({ length: MANIFEST_FETCH_CONCURRENCY * 3 }, (_, index) => (
        getRunManifest(run.run_id, `folder-${index}`)
      )),
    );

    expect(maximumActive).toBe(MANIFEST_FETCH_CONCURRENCY);
  });
});
