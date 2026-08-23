# Console Terraform root

This root deploys one Node 20 Lambda and one public Function URL. The Lambda zip
must exist at `frontend/build/openci-tf-console.zip`; create it from the repository
root with:

```sh
npm --prefix frontend ci
npm --prefix frontend run package:lambda
```

The archive contains `server-dist/`, `dist/`, production `node_modules/`, and
`package.json` at its root. The configured handler is
`server-dist/lambda.handler`, and static assets are read from `/var/task/dist`.
The static login shell/assets are public; every `/api/*` request requires the
console bearer token.

The Lambda environment contains only the exact SSM parameter name
`/openci-tf/install/<project>/console_token`. At cold start the server fetches and
decrypts that SecureString and caches it for the execution environment, keeping
the plaintext out of Terraform state. Create or rotate the value through stdin,
never as a command-line argument:

```sh
just config set-stdin console_token
```

The root discovers the existing `<project>-webhook` HTTP API by name. Its role
can write only to the console's own log group, call `ssm:GetParameter` on that
one token parameter, and use `execute-api:Invoke` on the core `/runs` routes plus
the four read-only admin routes. The token uses SSM's default AWS-managed key,
so no customer-key KMS permission is required.
