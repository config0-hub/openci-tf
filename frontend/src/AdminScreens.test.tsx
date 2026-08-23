// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AccountRow, GateLedger } from "./AdminScreens";
import type { GatesResponse } from "./admin-contract";

const sha = "0123456789abcdef0123456789abcdef01234567";

describe("documented admin response rendering", () => {
  it("renders the stored account role name without invented ARN fields", () => {
    const html = renderToStaticMarkup(
      <ol><AccountRow account={{ alias: "production", account_id: "123456789012", role_name: "openci-tf-executor-remote" }} index={0} /></ol>,
    );

    expect(html).toContain("production");
    expect(html).toContain("123456789012");
    expect(html).toContain("ROLE NAME");
    expect(html).toContain("openci-tf-executor-remote");
    expect(html).not.toContain("HUB ROLE");
    expect(html).not.toContain("TARGET ROLE");
  });

  it("renders latest-run gate observations with SHA and observed-at provenance", () => {
    const gates: GatesResponse = {
      enable_apply: false,
      folders_source: "latest-run-observation",
      folders: [{
        repo_name: "org/repo",
        folder: "infra/vpc",
        trigger_id: "repo-prod",
        run_id: "run-123",
        source_sha: sha,
        apply: true,
        destroy: false,
        observed_at: 1_700_000_000,
      }],
    };
    const html = renderToStaticMarkup(<GateLedger gates={gates} />);

    expect(html).toContain("PER-FOLDER STATE OBSERVED AT LAST RUN");
    expect(html).toContain("SOURCE: PINNED REPO CONFIG");
    expect(html).toContain(sha);
    expect(html).toContain("14 Nov 2023");
    expect(html).toContain("run-123 · repo-prod");
    expect(html).not.toContain("NO FOLDER OPT-INS");
  });

  it("renders an empty observation page as unavailable rather than opted out", () => {
    const html = renderToStaticMarkup(
      <GateLedger gates={{ enable_apply: false, folders_source: "latest-run-observation", folders: [] }} />,
    );

    expect(html).toContain("PER-FOLDER GATE STATE UNAVAILABLE");
    expect(html).toContain("ABSENT ROW DOES NOT MEAN OPTED OUT");
    expect(html).not.toContain("NO FOLDER OPT-INS");
  });
});
