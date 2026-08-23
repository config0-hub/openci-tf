import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { handle } from "hono/aws-lambda";
import type { APIGatewayProxyEventV2 } from "hono/aws-lambda";
import { createConsoleApp } from "./app.js";
import { mockApiResponse } from "./mock.js";
import { buildProxyUrl, hasValidBearerToken, type AwsFetchClient } from "./proxy.js";

const token = "procedure-secret";
let staticRoot: string;

beforeAll(async () => {
  staticRoot = await mkdtemp(join(tmpdir(), "openci-tf-console-test-"));
  await writeFile(join(staticRoot, "index.html"), "<!doctype html><title>console login</title>");
  await writeFile(join(staticRoot, "favicon.svg"), "<svg>stable</svg>");
  await writeFile(join(staticRoot, "logo.png"), Buffer.from([0, 255, 1, 254]));
  const assets = join(staticRoot, "assets");
  await mkdir(assets);
  await writeFile(join(assets, "app-a1b2c3.js"), "console.log('hashed')");
});

afterAll(async () => {
  await rm(staticRoot, { recursive: true, force: true });
});

function lambdaEvent(path: string, authorization?: string): APIGatewayProxyEventV2 {
  return {
    version: "2.0",
    routeKey: "$default",
    rawPath: path,
    rawQueryString: "",
    headers: {
      host: "console.lambda-url.us-east-1.on.aws",
      ...(authorization ? { authorization } : {}),
    },
    body: null,
    isBase64Encoded: false,
    requestContext: {
      accountId: "anonymous",
      apiId: "console",
      domainName: "console.lambda-url.us-east-1.on.aws",
      domainPrefix: "console",
      http: {
        method: "GET",
        path,
        protocol: "HTTP/1.1",
        sourceIp: "127.0.0.1",
        userAgent: "vitest",
      },
      requestId: "test-request",
      routeKey: "$default",
      stage: "$default",
      time: "20/Aug/2026:00:00:00 +0000",
      timeEpoch: 1,
    },
  };
}

describe("buildProxyUrl", () => {
  it("keeps an API stage prefix, strips the console prefix, and preserves the query", () => {
    expect(
      buildProxyUrl(
        "https://api.example.com/prod",
        "http://console.local/api/runs/run-1/folders?cursor=a%2Fb&limit=25",
      ),
    ).toBe("https://api.example.com/prod/runs/run-1/folders?cursor=a%2Fb&limit=25");
  });
});

describe("hasValidBearerToken", () => {
  it("accepts only the exact shared bearer token", () => {
    expect(hasValidBearerToken(`Bearer ${token}`, token)).toBe(true);
    expect(hasValidBearerToken("Bearer procedure-secrex", token)).toBe(false);
    expect(hasValidBearerToken(`Basic ${token}`, token)).toBe(false);
    expect(hasValidBearerToken(undefined, token)).toBe(false);
  });
});

describe("console app boundary", () => {
  it("serves the login shell and assets without auth but protects every API request", async () => {
    const signer: AwsFetchClient = {
      fetch: async () => new Response("proxied", { status: 200 }),
    };
    const app = createConsoleApp({
      consoleToken: token,
      staticRoot,
      apiBase: "https://api.example.com/prod",
      signer,
    });

    const shell = await app.request("/");
    expect(shell.status).toBe(200);
    expect(await shell.text()).toContain("console login");
    expect(shell.headers.get("Cache-Control")).toBe("no-store");

    const asset = await app.request("/assets/app-a1b2c3.js");
    expect(asset.status).toBe(200);
    expect(asset.headers.get("Cache-Control")).toBe("public, max-age=31536000, immutable");

    const stableAsset = await app.request("/favicon.svg");
    expect(stableAsset.status).toBe(200);
    expect(stableAsset.headers.get("Cache-Control")).toBe("no-cache");

    const unauthorized = await app.request("/api/runs?trigger_id=payments");
    expect(unauthorized.status).toBe(401);
    expect(unauthorized.headers.get("WWW-Authenticate")).toContain("Bearer");

    const authorized = await app.request("/api/runs?trigger_id=payments", {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(authorized.status).toBe(200);
    expect(await authorized.text()).toBe("proxied");
  });

  it("preserves method, body, path, query, redirects, and response while stripping browser credentials", async () => {
    let forwardedUrl: string | undefined;
    let forwardedInit: RequestInit | undefined;
    const signer: AwsFetchClient = {
      fetch: async (input, init) => {
        forwardedUrl = input.toString();
        forwardedInit = init;
        return new Response("upstream-body", {
          status: 307,
          headers: { Location: "/elsewhere", "X-Upstream": "preserved" },
        });
      },
    };
    const app = createConsoleApp({
      consoleToken: token,
      staticRoot,
      apiBase: "https://api.example.com/prod",
      signer,
    });
    const requestBody = JSON.stringify({ action: "plan", folder: "infra/api" });

    const response = await app.request("/api/runs?cursor=a%2Fb&limit=25", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
        "X-Amz-Date": "browser-controlled",
        "X-Amz-Security-Token": "browser-credential",
        "X-Trace": "keep-me",
      },
      body: requestBody,
    });

    expect(forwardedUrl).toBe("https://api.example.com/prod/runs?cursor=a%2Fb&limit=25");
    expect(forwardedInit?.method).toBe("POST");
    expect(forwardedInit?.redirect).toBe("manual");
    expect(Buffer.from(forwardedInit?.body as ArrayBuffer).toString()).toBe(requestBody);
    const headers = forwardedInit?.headers as Headers;
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("x-amz-date")).toBeNull();
    expect(headers.get("x-amz-security-token")).toBeNull();
    expect(headers.get("x-trace")).toBe("keep-me");
    expect(response.status).toBe(307);
    expect(response.headers.get("Location")).toBe("/elsewhere");
    expect(response.headers.get("X-Upstream")).toBe("preserved");
    expect(await response.text()).toBe("upstream-body");
  });

  it("returns a bounded error when the upstream fetch fails", async () => {
    const signer: AwsFetchClient = {
      fetch: async () => {
        throw new TypeError(`network failure ${"secret-detail".repeat(1000)}`);
      },
    };
    const app = createConsoleApp({
      consoleToken: token,
      staticRoot,
      apiBase: "https://api.example.com/prod",
      signer,
    });

    const response = await app.request("/api/runs", {
      headers: { Authorization: `Bearer ${token}` },
    });
    const responseBody = await response.text();
    expect(response.status).toBe(502);
    expect(responseBody.length).toBeLessThan(100);
    expect(responseBody).not.toContain("secret-detail");
  });

  it("preserves the public-static/private-api boundary and binary media through the Lambda adapter", async () => {
    const app = createConsoleApp({ consoleToken: token, staticRoot, mockApiHandler: mockApiResponse });
    const lambdaHandler = handle(app);

    const shell = await lambdaHandler(lambdaEvent("/"));
    expect(shell.statusCode).toBe(200);
    expect(shell.body).toContain("console login");

    const unauthorized = await lambdaHandler(lambdaEvent("/api/repos"));
    expect(unauthorized.statusCode).toBe(401);

    const authorized = await lambdaHandler(lambdaEvent("/api/repos", `Bearer ${token}`));
    expect(authorized.statusCode).toBe(200);

    const image = await lambdaHandler(lambdaEvent("/logo.png"));
    expect(image.statusCode).toBe(200);
    expect(image.isBase64Encoded).toBe(true);
    expect(Buffer.from(image.body, "base64")).toEqual(Buffer.from([0, 255, 1, 254]));
    expect(image.headers["cache-control"]).toBe("no-cache");
  });
});
