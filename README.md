# openci-tf

Safe-path CI for Terraform/OpenTofu in GitHub pull requests.

openci-tf runs authenticated `tf plan` and `tf report` commands
against registered repositories and AWS accounts. Read-only work runs through a
readonly execution lane; apply and destroy require the separate gated flow
documented in [`docs/APPLY.md`](docs/APPLY.md).

Start with [`docs/INSTALL.md`](docs/INSTALL.md) for deployment and
[`docs/API.md`](docs/API.md) for the core API.

## License

Copyright © 2026 Config0, Inc.

Licensed under [AAGPL-3.0-or-later](LICENSE).
