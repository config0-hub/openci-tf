import { describe, expect, it } from "vitest";
import { loadConsoleToken } from "./token.js";
import type { AwsFetchClient } from "./proxy.js";

describe("loadConsoleToken", () => {
  it("uses CONSOLE_TOKEN as the local development override", async () => {
    await expect(loadConsoleToken({ CONSOLE_TOKEN: "local-token" })).resolves.toBe("local-token");
  });

  it("fetches and decrypts the exact SSM parameter at runtime", async () => {
    let requestedUrl: string | undefined;
    let requestedInit: RequestInit | undefined;
    const client: AwsFetchClient = {
      fetch: async (input, init) => {
        requestedUrl = input.toString();
        requestedInit = init;
        return Response.json({ Parameter: { Value: "runtime-token" } });
      },
    };

    await expect(loadConsoleToken({
      AWS_REGION: "us-east-1",
      CONSOLE_TOKEN_PARAMETER: "/openci-tf/install/acme/console_token",
    }, client)).resolves.toBe("runtime-token");

    expect(requestedUrl).toBe("https://ssm.us-east-1.amazonaws.com/");
    expect(requestedInit?.method).toBe("POST");
    expect(requestedInit?.redirect).toBe("manual");
    expect(JSON.parse(requestedInit?.body as string)).toEqual({
      Name: "/openci-tf/install/acme/console_token",
      WithDecryption: true,
    });
    const headers = requestedInit?.headers as Record<string, string>;
    expect(headers["X-Amz-Target"]).toBe("AmazonSSM.GetParameter");
  });

  it("does not include an SSM response body in initialization errors", async () => {
    const client: AwsFetchClient = {
      fetch: async () => new Response("sensitive-provider-detail", { status: 403 }),
    };

    await expect(loadConsoleToken({
      AWS_REGION: "us-east-1",
      CONSOLE_TOKEN_PARAMETER: "/openci-tf/install/acme/console_token",
    }, client)).rejects.toThrow("SSM GetParameter failed with HTTP 403");
  });
});
