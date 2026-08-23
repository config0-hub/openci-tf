# openci-tf console

Phase 1 is a Vite/React console plus a small Hono Node server. The server serves
the static login shell and assets publicly, protects every `/api/*` request with
the shared console bearer token, SigV4-signs those requests for API Gateway,
serves `dist/`, and exports `handler` from `server-dist/lambda.js` for
`hono/aws-lambda`.

## Prerequisites

- Node 20 or newer
- npm
- For real API mode, environment-provided AWS credentials authorized for the
  existing openci-tf API Gateway routes

Install once:

```bash
cd frontend
npm install
```

## Local mock mode

Run the API proxy and Vite in two terminals. The token below is fixture-only.

Terminal 1:

```bash
cd frontend
npm run build:server
CONSOLE_TOKEN=local-console-token CONSOLE_MOCK_API=1 npm start
```

Terminal 2:

```bash
cd frontend
VITE_CONSOLE_MOCK=true npm run dev
```

Open <http://127.0.0.1:5173>, enter `local-console-token`, and use these fixture
views:

- `/` — cross-trigger in-progress, complete, failed, detected/clean/unknown drift, and expired-plan runs
- `/?trigger_id=empty` — empty registry
- `/runs/run-progress` — polling/in-progress detail
- `/runs/run-complete` — complete folders and artifacts
- `/runs/run-failed` — failure with corrective evidence
- `/runs/run-drift` — authoritative drift-detected procedure
- `/runs/run-drift-clean` — authoritative clean drift procedure
- `/runs/run-drift-unknown` — migration-safe unknown drift result
- `/runs/run-expired-plan` — explicit expired binary-plan state

## Local real API mode

The browser still runs through Vite, which proxies `/api` to Hono. Supply
short-lived AWS environment credentials to the Hono process.

Terminal 1:

```bash
cd frontend
npm run build:server
CONSOLE_TOKEN='<shared-console-token>' \
OPENCI_TF_API_BASE='https://<api-id>.execute-api.<region>.amazonaws.com/<stage>' \
AWS_REGION='<region>' \
AWS_ACCESS_KEY_ID='<access-key-id>' \
AWS_SECRET_ACCESS_KEY='<secret-access-key>' \
AWS_SESSION_TOKEN='<session-token-if-present>' \
npm start
```

Terminal 2:

```bash
cd frontend
npm run dev
```

Open <http://127.0.0.1:5173> and enter the same shared console token. Runs lists
all trigger partitions authorized by the caller policy; `trigger_id` and `repo`
are optional server-side filters.

## Production build and checks

```bash
cd frontend
npm run typecheck
npm test
npm run build
```

Create the Lambda deployment artifact with:

```bash
cd frontend
npm run package:lambda
```

The packaging script runs the production build, installs only locked runtime
npm dependencies into an isolated staging directory, and writes
`frontend/build/openci-tf-console.zip`. The zip has `server-dist/`, `dist/`,
`node_modules/`, and `package.json` at its root; Terraform uses
`server-dist/lambda.handler` and serves static assets from `/var/task/dist`.
Generated staging files and the zip are gitignored.

Run the built Hono app locally with `npm start`. `CONSOLE_STATIC_ROOT` may point
to a different built-asset directory; it defaults to `frontend/dist` when run
from this directory. `CONSOLE_TOKEN` is the local-development override. In the
Lambda deployment, only `CONSOLE_TOKEN_PARAMETER` is set; the server fetches and
decrypts that exact SSM SecureString once at cold start. The Hono server serves
static files without auth and enforces `Authorization: Bearer <token>` on every
`/api/*` request.

## Contract notes

- Runs are listed across authorized trigger partitions with server-side trigger
  and repository filtering plus stable cursor pagination.
- Run records may omit folder count, and manifests do not expose authoritative
  per-step outcomes. The UI renders an em dash for an unavailable count and
  labels artifact presence as `FILED`; it never converts output existence into a
  successful step outcome.
- Drift classification comes only from `drift_detected`. A successful legacy row
  without that field remains `COMPLETE` with a visible unknown-result note.
- Gate folder rows are retained latest-run observations of pinned repository
  config. Their SHA/run/time provenance is visible and absence is not rendered as
  an opt-out.

No dependencies beyond those authorized in the Phase 1 task are used. The
self-hosted Barlow Condensed font files are licensed under the OFL included at
`public/fonts/OFL.txt`.
