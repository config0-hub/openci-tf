# Claude Code — openci-tf

## Verification

After Python or Terraform-in-test changes, run `just test` from the repo root
and read the container output. That image is Python 3.14 and includes the
Terraform CLI used by module/fixture tests.

## Delegation

Long waits (image builds, `just install`) may go to a subagent so the main
session stays usable. Keep apply/destroy and any remote git mutation in the
session the human is talking to.
