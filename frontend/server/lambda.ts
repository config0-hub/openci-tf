import { handle } from "hono/aws-lambda";
import { createRuntimeApp } from "./app.js";

// Top-level initialization fetches and decrypts the SSM token once per cold
// start. Local development can override the lookup with CONSOLE_TOKEN.
const app = await createRuntimeApp();

export const handler = handle(app);
