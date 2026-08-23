#!/usr/bin/env bash
# SigV4-invoke the openci-tf HTTP API (IAM auth). Usage: api_invoke.sh <method> <path> [json-body]
set -euo pipefail
method="${1:?method required}"
path="${2:?path required}"
body="${3:-}"
url="$(terraform -chdir=infra/deploy output -raw api_url 2>/dev/null || true)"
[ -n "$url" ] || { echo "ERROR: api_url output missing; run just deploy" >&2; exit 1; }
target="${url%/}${path}"
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
python3 - "$method" "$target" "$region" "$body" <<'PY'
import json, os, sys
import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
import urllib.request

method, url, region, body = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
headers = {"Content-Type": "application/json"}
data = body.encode() if body else None
request = AWSRequest(method=method, url=url, data=data, headers=headers)
SigV4Auth(botocore.session.get_session().get_credentials(), "execute-api", region).add_auth(request)
prepared = request.prepare()
req = urllib.request.Request(prepared.url, data=prepared.body, method=method)
for key, value in prepared.headers.items():
    req.add_header(key, value)
with urllib.request.urlopen(req) as response:
    sys.stdout.write(response.read().decode())
PY
