# Sample repository fixture

`sample-target-repo/` is a tracked Terraform sample used by the unit suite to
exercise folder discovery, repository configuration parsing, and plan command
construction across same-account and cross-account folder layouts.

The fixture intentionally includes multiple regions and two account aliases so
resolver tests cover realistic repository structure without requiring AWS access.
`sample-target-repo.snapshot.json` records file digests so tests can detect
accidental fixture drift.

This fixture is for local regression testing. Running against real AWS still
requires a registered repository, configured accounts, and normal openci-tf
operator setup.
