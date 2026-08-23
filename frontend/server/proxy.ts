import { createHash, timingSafeEqual } from "node:crypto";

export interface AwsFetchClient {
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>;
}

export function hasValidBearerToken(header: string | undefined, expected: string): boolean {
  if (!header?.startsWith("Bearer ")) return false;
  const presentedDigest = createHash("sha256").update(header.slice(7), "utf8").digest();
  const expectedDigest = createHash("sha256").update(expected, "utf8").digest();
  return timingSafeEqual(presentedDigest, expectedDigest);
}

export function buildProxyUrl(apiBase: string, requestUrl: string): string {
  const base = new URL(apiBase.endsWith("/") ? apiBase : `${apiBase}/`);
  const incoming = new URL(requestUrl);
  const downstreamPath = incoming.pathname.replace(/^\/api(?=\/|$)/, "") || "/";
  base.pathname = `${base.pathname.replace(/\/$/, "")}${downstreamPath}`;
  base.search = incoming.search;
  base.hash = "";
  return base.toString();
}

export async function proxyToApi(
  request: Request,
  apiBase: string,
  client: AwsFetchClient,
): Promise<Response> {
  const headers = new Headers(request.headers);
  for (const name of [
    "authorization",
    "connection",
    "content-length",
    "host",
    "x-amz-content-sha256",
    "x-amz-date",
    "x-amz-security-token",
  ]) {
    headers.delete(name);
  }
  const body = request.method === "GET" || request.method === "HEAD"
    ? undefined
    : await request.arrayBuffer();

  try {
    return await client.fetch(buildProxyUrl(apiBase, request.url), {
      method: request.method,
      headers,
      body,
      redirect: "manual",
    });
  } catch (error: unknown) {
    if (!(error instanceof TypeError)) throw error;
    return new Response(JSON.stringify({ error: "upstream API unavailable" }), {
      status: 502,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  }
}
