# openci-tf — Python

Python 3.14 (Lambda base image and `docker/Dockerfile.test`). Modern typing:
`list[str]`, `str | None`, `match`. No `Optional[X]`.

This repo is one service, not a multi-package monorepo. Do not invent
`src/packages/` or a private PyPI overlay.

## Layout

```
src/core/         # primitives (logging, small shared types)
src/domain/       # rules, grammar, gates — no AWS I/O
src/platform/     # AWS, GitHub, git adapters
src/services/     # Lambda handlers and use-case orchestration
tests/unit/       # pytest
```

Imports point inward: `services` → `domain`/`platform`/`core`. Domain does not
import `services` or call AWS. Platform does not import `services`.

Handlers keep `__all__` empty — they are entrypoints, not libraries.

## Models and I/O

- Type-annotate function signatures. Use `from __future__ import annotations`.
- Domain shapes are dataclasses (see `src/domain/intent/models.py`). Do not
  introduce an ORM.
- AWS via boto3; HTTP via `requests`. Pin majors in `requirements.txt`.

## Errors

Fail loud. Catch a named exception you can handle, and keep `try` to the
line that can raise. No `except Exception`, no bare `except:`, no catch-to-`None`.

Webhook, resolve, and apply-gate paths are fail-closed: a missing setting, bad
signature, or failed gate is an error response or a raised error, not a default
that continues the run.

## Tests

```bash
just test
```

That copies `ENGINE_REPO_PATH/aws_exe_sys/common/payload.py` into
`docker/engine_ref/payload.py`, builds `docker/Dockerfile.test`, and runs
pytest on `tests/`. The image includes Terraform 1.12.2 for module/fixture
tests.

- Unit tests mock at the AWS/GitHub boundary.
- Do not call live AWS or GitHub from unit tests.
- Run the suite twice before calling a change green if the change is in
  engine payload, Terraform fixtures, or Docker test plumbing.

## Execution loop

1. Read the existing module and its tests — match local style (SPDX header,
   dataclass vs dict) before adding a new pattern.
2. Keep new logic in the layer it belongs to (domain vs platform vs service).
3. Add or update `tests/unit/` for the behaviour.
4. `just test`
5. Deliver only when the container run is green.
