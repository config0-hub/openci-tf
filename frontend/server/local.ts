// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { createRuntimeApp } from "./app.js";

interface WebApplication {
  fetch(request: Request): Response | Promise<Response>;
}

async function requestBody(request: IncomingMessage): Promise<Buffer | undefined> {
  if (request.method === "GET" || request.method === "HEAD") return undefined;
  const chunks: Buffer[] = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

async function serve(
  application: WebApplication,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  const headers = new Headers();
  for (const [name, value] of Object.entries(request.headers)) {
    if (Array.isArray(value)) value.forEach((item) => headers.append(name, item));
    else if (value !== undefined) headers.set(name, value);
  }
  const body = await requestBody(request);
  const requestTarget = request.url?.startsWith("/") ? request.url : "/";
  const webRequest = new Request(`http://127.0.0.1${requestTarget}`, {
    method: request.method,
    headers,
    body: body as unknown as BodyInit | undefined,
  });
  const webResponse = await application.fetch(webRequest);
  response.statusCode = webResponse.status;
  webResponse.headers.forEach((value, name) => response.setHeader(name, value));
  response.end(Buffer.from(await webResponse.arrayBuffer()));
}

function respondToRejectedRequest(response: ServerResponse, error: unknown): void {
  const isMalformedRequest = error instanceof TypeError;
  if (!isMalformedRequest) console.error("local console request failed", error);
  if (response.writableEnded) return;
  if (!response.headersSent) {
    response.statusCode = isMalformedRequest ? 400 : 500;
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    response.setHeader("Cache-Control", "no-store");
  }
  response.end(JSON.stringify({ error: isMalformedRequest ? "invalid request" : "request failed" }));
}

export function createLocalServer(application: WebApplication): Server {
  return createServer((request, response) => {
    void serve(application, request, response).catch((error: unknown) => {
      respondToRejectedRequest(response, error);
    });
  });
}

async function main(): Promise<void> {
  // Mock wiring lives only in this dev entry: the lambda entry never imports
  // mock code, so fixtures cannot reach the deployed console.
  const mockApiHandler = process.env.CONSOLE_MOCK_API === "1"
    ? (await import("./mock.js")).mockApiResponse
    : undefined;
  const application = await createRuntimeApp(mockApiHandler);
  const port = Number(process.env.PORT ?? 8787);
  createLocalServer(application).listen(port, "127.0.0.1", () => {
    console.log(`openci-tf console listening on http://127.0.0.1:${port}`);
  });
}

const invokedPath = process.argv[1];
if (invokedPath && import.meta.url === pathToFileURL(resolve(invokedPath)).href) {
  void main().catch((error: unknown) => {
    console.error("failed to start local console", error);
    process.exitCode = 1;
  });
}
