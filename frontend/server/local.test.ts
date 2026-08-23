// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { connect } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { AddressInfo } from "node:net";
import { describe, expect, it } from "vitest";
import { createConsoleApp } from "./app.js";
import { createLocalServer } from "./local.js";
import { mockApiResponse } from "./mock.js";

function rawRequest(port: number, request: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = connect(port, "127.0.0.1");
    const chunks: Buffer[] = [];
    socket.on("connect", () => socket.write(request));
    socket.on("data", (chunk: Buffer) => chunks.push(chunk));
    socket.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    socket.on("error", reject);
  });
}

describe("local server", () => {
  it("ignores a malformed Host header and remains available", async () => {
    const staticRoot = await mkdtemp(join(tmpdir(), "openci-tf-local-test-"));
    await mkdir(join(staticRoot, "assets"));
    await writeFile(join(staticRoot, "index.html"), "local console");
    const app = createConsoleApp({ consoleToken: "test-token", staticRoot, mockApiHandler: mockApiResponse });
    const server = createLocalServer(app);
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
    const port = (server.address() as AddressInfo).port;

    try {
      const malformedHostResponse = await rawRequest(
        port,
        "GET / HTTP/1.1\r\nHost: [\r\nConnection: close\r\n\r\n",
      );
      expect(malformedHostResponse).toContain("200 OK");
      expect(malformedHostResponse).toContain("local console");

      const subsequent = await fetch(`http://127.0.0.1:${port}/`);
      expect(subsequent.status).toBe(200);
      expect(await subsequent.text()).toBe("local console");
    } finally {
      await new Promise<void>((resolve, reject) => {
        server.close((error) => error ? reject(error) : resolve());
      });
      await rm(staticRoot, { recursive: true, force: true });
    }
  });
});
