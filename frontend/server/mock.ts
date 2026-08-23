import {
  emptyMockAccounts,
  emptyMockGates,
  emptyMockLocks,
  emptyMockRepos,
  mockAccounts,
  mockFolders,
  mockGates,
  mockLocks,
  mockManifests,
  mockRepos,
  mockRuns,
} from "./mock-data.js";

const json = (body: unknown, status = 200) => Response.json(body, {
  status,
  headers: { Date: new Date().toUTCString() },
});
const submittedRunIds = new Map<string, string>();
let submissionSequence = 0;

interface MockCreateRunRequest {
  trigger_id: string;
  commit_hash: string;
  action: "plan" | "drift" | "report";
  folder_mode: "all" | "explicit";
  folders: string[];
  idempotency_key: string;
  notification_target: { type: "registry" };
}

function boundedLimit(url: URL): number {
  const parsed = Number.parseInt(url.searchParams.get("limit") ?? "25", 10);
  if (!Number.isFinite(parsed)) return 25;
  return Math.min(Math.max(1, parsed), 100);
}

function mockPage<T>(url: URL, items: T[], prefix: string): { items: T[]; cursor?: string } {
  const rawCursor = url.searchParams.get("cursor");
  let offset = 0;
  if (rawCursor) {
    const match = new RegExp(`^${prefix}:(\\d+)$`).exec(rawCursor);
    if (!match) return { items: [] };
    offset = Number.parseInt(match[1], 10);
  }
  const limit = boundedLimit(url);
  const page = items.slice(offset, offset + limit);
  const nextOffset = offset + page.length;
  return nextOffset < items.length
    ? { items: page, cursor: `${prefix}:${nextOffset}` }
    : { items: page };
}

function isCreateRunRequest(value: unknown): value is MockCreateRunRequest {
  if (!value || typeof value !== "object") return false;
  const request = value as Record<string, unknown>;
  const notification = request.notification_target;
  const folders = request.folders;
  return typeof request.trigger_id === "string" && request.trigger_id.trim().length > 0
    && typeof request.commit_hash === "string" && /^[0-9a-fA-F]{40}$/.test(request.commit_hash)
    && ["plan", "drift", "report"].includes(String(request.action))
    && ["all", "explicit"].includes(String(request.folder_mode))
    && Array.isArray(folders) && folders.every((folder) => typeof folder === "string" && folder.trim().length > 0)
    && (request.folder_mode !== "explicit" || folders.length > 0)
    && typeof request.idempotency_key === "string" && request.idempotency_key.trim().length >= 8
    && Boolean(notification) && typeof notification === "object"
    && (notification as Record<string, unknown>).type === "registry";
}

export async function mockApiResponse(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const path = url.pathname.replace(/^\/api/, "");
  const emptyFixture = url.searchParams.get("fixture") === "empty";

  if (request.method === "GET" && path === "/repos") {
    const page = mockPage(url, emptyFixture ? emptyMockRepos : mockRepos, "repos");
    return json({ repos: page.items, ...(page.cursor ? { cursor: page.cursor } : {}) });
  }
  if (request.method === "GET" && path === "/accounts") {
    const page = mockPage(url, emptyFixture ? emptyMockAccounts : mockAccounts, "accounts");
    return json({ accounts: page.items, ...(page.cursor ? { cursor: page.cursor } : {}) });
  }
  if (request.method === "GET" && path === "/locks") {
    const page = mockPage(url, emptyFixture ? emptyMockLocks : mockLocks, "locks");
    return json({ locks: page.items, ...(page.cursor ? { cursor: page.cursor } : {}) });
  }
  if (request.method === "GET" && path === "/gates") {
    const gates = emptyFixture ? emptyMockGates : mockGates;
    const page = mockPage(url, gates.folders, "gates");
    return json({
      ...gates,
      folders: page.items,
      ...(page.cursor ? { cursor: page.cursor } : {}),
    });
  }

  if (request.method === "POST" && path === "/runs") {
    const body: unknown = await request.json();
    if (!isCreateRunRequest(body)) return json({ error: "invalid create-run procedure" }, 400);
    const repo = mockRepos.find((item) => item.trigger_ids.includes(body.trigger_id));
    if (!repo) return json({ error: `unknown trigger_id: ${body.trigger_id}` }, 404);
    const idempotencyId = `${body.trigger_id}:${body.idempotency_key.trim()}`;
    const existing = submittedRunIds.get(idempotencyId);
    if (existing) return json({ run_id: existing }, 202);

    const createdAt = Math.floor(Date.now() / 1_000);
    const runId = `run-console-${createdAt.toString(36)}-${(++submissionSequence).toString(36)}`;
    const folders = body.folder_mode === "explicit"
      ? [...new Set(body.folders.map((folder) => folder.trim()))]
      : mockGates.folders.filter((folder) => folder.repo_name === repo.repo_name).map((folder) => folder.folder);
    mockRuns.unshift({
      run_id: runId,
      trigger_id: body.trigger_id.trim(),
      repo_name: repo.repo_name,
      commit_hash: body.commit_hash.toLocaleLowerCase(),
      action: body.action,
      status: "accepted",
      folder_count: folders.length,
      created_at: createdAt,
      updated_at: createdAt,
      expire_ttl: createdAt + 90 * 86_400,
    });
    mockFolders[runId] = folders.map((folder, index) => ({
      run_id: runId,
      folder,
      folder_id: Buffer.from(folder).toString("base64url"),
      account_id: "123456789012",
      execution_id: `${runId}.${index}.0`,
      attempt: 0,
      status: "accepted",
      updated_at: createdAt,
      expire_ttl: createdAt + 90 * 86_400,
    }));
    submittedRunIds.set(idempotencyId, runId);
    return json({ run_id: runId }, 202);
  }

  if (request.method === "GET" && path === "/runs") {
    const triggerId = url.searchParams.get("trigger_id") ?? "";
    const repo = (url.searchParams.get("repo") ?? "").toLocaleLowerCase();
    if (triggerId === "empty") return json({ runs: [] });
    const runs = mockRuns.filter((run) =>
      (!triggerId || run.trigger_id === triggerId) &&
      (!repo || run.repo_name.toLocaleLowerCase().includes(repo)),
    );
    const cursor = url.searchParams.get("cursor");
    const cursorIndex = cursor ? runs.findIndex((run) => run.run_id === cursor) : -1;
    const offset = cursorIndex >= 0 ? cursorIndex + 1 : 0;
    const limit = boundedLimit(url);
    const page = runs.slice(offset, offset + limit);
    const nextCursor = offset + page.length < runs.length ? page.at(-1)?.run_id : undefined;
    return json({ runs: page, ...(nextCursor ? { cursor: nextCursor } : {}) });
  }

  const foldersMatch = path.match(/^\/runs\/([^/]+)\/folders$/);
  if (request.method === "GET" && foldersMatch) {
    const runId = decodeURIComponent(foldersMatch[1]);
    const folders = mockFolders[runId];
    return folders ? json({ folders }) : json({ error: "run not found or expired" }, 404);
  }

  const manifestMatch = path.match(/^\/runs\/([^/]+)\/folders\/([^/]+)\/manifest$/);
  if (request.method === "GET" && manifestMatch) {
    const runId = decodeURIComponent(manifestMatch[1]);
    const record = mockFolders[runId]?.find((item) => item.folder_id === manifestMatch[2]);
    if (!record) return json({ error: "folder execution not found or expired" }, 404);
    const manifest = mockManifests[`${runId}/${record.folder}`];
    return manifest ? json(manifest) : json({ error: "manifest not available" }, 404);
  }

  const runMatch = path.match(/^\/runs\/([^/]+)$/);
  if (request.method === "GET" && runMatch) {
    const run = mockRuns.find((item) => item.run_id === decodeURIComponent(runMatch[1]));
    return run ? json(run) : json({ error: "run not found or expired" }, 404);
  }

  return json({ error: `mock route not found: ${request.method} ${path}` }, 404);
}
