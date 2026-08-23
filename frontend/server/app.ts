import { existsSync, statSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { resolve, sep } from "node:path";
import { AwsClient } from "aws4fetch";
import { Hono, type Context, type MiddlewareHandler } from "hono";
import { hasValidBearerToken, proxyToApi, type AwsFetchClient } from "./proxy.js";
import { loadConsoleToken } from "./token.js";

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function awsClient(service: string): AwsClient {
  return new AwsClient({
    accessKeyId: required("AWS_ACCESS_KEY_ID"),
    secretAccessKey: required("AWS_SECRET_ACCESS_KEY"),
    sessionToken: process.env.AWS_SESSION_TOKEN,
    region: required("AWS_REGION"),
    service,
  });
}

const contentTypes: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".webp": "image/webp",
};

function contentType(path: string): string {
  const dot = path.lastIndexOf(".");
  return contentTypes[dot >= 0 ? path.slice(dot) : ""] ?? "application/octet-stream";
}

export type ApiHandler = (request: Request) => Response | Promise<Response>;

export interface ConsoleAppOptions {
  consoleToken: string;
  staticRoot: string;
  /**
   * Dev-only replacement for the signed API proxy. The lambda entry never
   * passes this, so mock fixtures stay out of the deployed bundle.
   */
  mockApiHandler?: ApiHandler;
  apiBase?: string;
  signer?: AwsFetchClient;
}

export function createConsoleApp(options: ConsoleAppOptions): Hono {
  const app = new Hono();
  const staticRoot = resolve(options.staticRoot);

  const authorizeApi: MiddlewareHandler = async (context, next) => {
    if (!hasValidBearerToken(context.req.header("Authorization"), options.consoleToken)) {
      return context.json({ error: "missing or invalid console bearer token" }, 401, {
        "WWW-Authenticate": "Bearer realm=\"openci-tf console\"",
        "Cache-Control": "no-store",
      });
    }
    await next();
  };

  app.use("/api", authorizeApi);
  app.use("/api/*", authorizeApi);

  const apiHandler = async (context: Context): Promise<Response> => {
    if (options.mockApiHandler) return options.mockApiHandler(context.req.raw);
    if (!options.signer || !options.apiBase) throw new Error("API proxy configuration is unavailable");
    return proxyToApi(context.req.raw, options.apiBase, options.signer);
  };
  app.all("/api", apiHandler);
  app.all("/api/*", apiHandler);

  app.get("*", async (context) => {
    const pathname = new URL(context.req.url).pathname;
    const requested = pathname === "/" ? "/index.html" : pathname;
    const candidate = resolve(staticRoot, `.${requested}`);
    if (!candidate.startsWith(`${staticRoot}${sep}`)) return context.text("not found", 404);
    const index = resolve(staticRoot, "index.html");
    const asset = existsSync(candidate) && statSync(candidate).isFile() ? candidate : index;
    if (!existsSync(asset)) return context.text("console build not found", 503);
    const body = await readFile(asset);
    const cacheControl = asset === index
      ? "no-store"
      : requested.startsWith("/assets/")
        ? "public, max-age=31536000, immutable"
        : "no-cache";
    return new Response(body, {
      headers: {
        "Content-Type": contentType(asset),
        "Cache-Control": cacheControl,
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
      },
    });
  });

  return app;
}

export async function createRuntimeApp(mockApiHandler?: ApiHandler): Promise<Hono> {
  const consoleToken = await loadConsoleToken(process.env, mockApiHandler ? undefined : awsClient("ssm"));
  return createConsoleApp({
    consoleToken,
    mockApiHandler,
    staticRoot: process.env.CONSOLE_STATIC_ROOT ?? "dist",
    apiBase: mockApiHandler ? undefined : required("OPENCI_TF_API_BASE"),
    signer: mockApiHandler ? undefined : awsClient("execute-api"),
  });
}
