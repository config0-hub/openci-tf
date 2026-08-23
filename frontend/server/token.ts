// SPDX-FileCopyrightText: 2026 Config0, Inc.
// SPDX-License-Identifier: AGPL-3.0-or-later
import type { AwsFetchClient } from "./proxy.js";

interface ConsoleEnvironment {
  AWS_REGION?: string;
  CONSOLE_TOKEN?: string;
  CONSOLE_TOKEN_PARAMETER?: string;
}

interface GetParameterResponse {
  Parameter?: {
    Value?: unknown;
  };
}

export async function loadConsoleToken(
  environment: ConsoleEnvironment,
  client?: AwsFetchClient,
): Promise<string> {
  if (environment.CONSOLE_TOKEN) return environment.CONSOLE_TOKEN;

  const parameterName = environment.CONSOLE_TOKEN_PARAMETER;
  if (!parameterName) throw new Error("CONSOLE_TOKEN_PARAMETER is required when CONSOLE_TOKEN is unset");
  const region = environment.AWS_REGION;
  if (!region) throw new Error("AWS_REGION is required when CONSOLE_TOKEN is unset");
  if (!client) throw new Error("an SSM signing client is required when CONSOLE_TOKEN is unset");

  const response = await client.fetch(`https://ssm.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AmazonSSM.GetParameter",
    },
    body: JSON.stringify({ Name: parameterName, WithDecryption: true }),
    redirect: "manual",
  });
  if (!response.ok) {
    throw new Error(`SSM GetParameter failed with HTTP ${response.status}`);
  }

  const payload = await response.json() as GetParameterResponse;
  const value = payload.Parameter?.Value;
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("SSM GetParameter returned an empty console token");
  }
  return value;
}
