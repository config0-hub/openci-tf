// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { describe, expect, it } from "vitest";
import { mockApiResponse } from "./mock.js";

const sha = "4e8a4fb977e7fdf2bc891a1e21470c52aa44b634";

async function body(response: Response): Promise<Record<string, unknown>> {
  return await response.json() as Record<string, unknown>;
}

describe("mock admin fixtures", () => {
  it.each([
    ["repos", "repos"],
    ["accounts", "accounts"],
    ["locks", "locks"],
  ])("returns populated and empty %s lists", async (route, key) => {
    const populated = await mockApiResponse(new Request(`http://console.local/api/${route}`));
    const populatedBody = await body(populated);
    expect(populated.status).toBe(200);
    expect((populatedBody[key] as unknown[]).length).toBeGreaterThan(0);

    const empty = await mockApiResponse(new Request(`http://console.local/api/${route}?fixture=empty`));
    expect((await body(empty))[key]).toEqual([]);
  });

  it("returns populated and empty gate states", async () => {
    const populated = await body(await mockApiResponse(new Request("http://console.local/api/gates")));
    expect(populated.enable_apply).toBe(true);
    expect((populated.folders as unknown[]).length).toBeGreaterThan(0);

    const empty = await body(await mockApiResponse(new Request("http://console.local/api/gates?fixture=empty")));
    expect(empty).toEqual({
      enable_apply: false,
      folders_source: "latest-run-observation",
      folders: [],
    });
  });

  it("returns bounded cursor pages for admin and gate contracts", async () => {
    for (const [route, key] of [["repos", "repos"], ["accounts", "accounts"], ["locks", "locks"], ["gates", "folders"]]) {
      const first = await body(await mockApiResponse(
        new Request(`http://console.local/api/${route}?limit=1`),
      ));
      expect((first[key] as unknown[])).toHaveLength(1);
      expect(first.cursor).toBe(`${route}:1`);

      const second = await body(await mockApiResponse(
        new Request(`http://console.local/api/${route}?limit=1&cursor=${first.cursor}`),
      ));
      expect((second[key] as unknown[])).toHaveLength(1);
    }
  });

  it("lists runs across authorized mock triggers with server-side repo filtering and one cursor", async () => {
    const first = await body(await mockApiResponse(
      new Request("http://console.local/api/runs?repo=acme&limit=1"),
    ));
    expect((first.runs as unknown[])).toHaveLength(1);
    expect(first.cursor).toBeTypeOf("string");

    const second = await body(await mockApiResponse(
      new Request(`http://console.local/api/runs?repo=acme&limit=1&cursor=${first.cursor}`),
    ));
    expect((second.runs as unknown[])).toHaveLength(1);
    expect((second.runs as Array<{ run_id: string }>)[0].run_id)
      .not.toBe((first.runs as Array<{ run_id: string }>)[0].run_id);
  });
});

describe("mock create run", () => {
  it("accepts a safe procedure, preserves idempotency, and exposes its detail", async () => {
    const requestBody = {
      trigger_id: "payments-prod",
      commit_hash: sha,
      action: "plan",
      folder_mode: "explicit",
      folders: ["infra/api"],
      idempotency_key: "mock-test-idempotency",
      notification_target: { type: "registry" },
    };
    const create = () => mockApiResponse(new Request("http://console.local/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    }));

    const first = await create();
    const firstBody = await body(first);
    expect(first.status).toBe(202);
    expect(firstBody.run_id).toMatch(/^run-console-/);

    const repeated = await create();
    expect(await body(repeated)).toEqual(firstBody);

    const detail = await mockApiResponse(new Request(`http://console.local/api/runs/${firstBody.run_id}`));
    expect(detail.status).toBe(200);
    expect((await body(detail)).status).toBe("accepted");
  });

  it("rejects a procedure outside the safe create-run contract", async () => {
    const response = await mockApiResponse(new Request("http://console.local/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ trigger_id: "payments-prod", commit_hash: sha, action: "unsupported" }),
    }));
    expect(response.status).toBe(400);
  });
});
