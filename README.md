# openci-tf

Safe-path CI for Terraform/OpenTofu in GitHub pull requests.

openci-tf runs authenticated `tf plan <folder-or-csv>`, `tf plan pipeline <name>`,
`tf drift pipeline <name>`, and `tf report` commands from pull request comments
against registered repositories and AWS accounts. Every command is recorded in a
durable audit comment on the PR; invalid commands get a short help comment and
are removed. Read-only work runs through a readonly execution lane; apply and
destroy require the separate gated flow documented in
[`docs/APPLY.md`](docs/APPLY.md). The command set and comment lifecycle are in
[`docs/GITHUB_WEBHOOK.md`](docs/GITHUB_WEBHOOK.md).

Start with [`docs/INSTALL.md`](docs/INSTALL.md) for deployment and
[`docs/API.md`](docs/API.md) for the core API.

## License

Copyright © 2026 Config0, Inc.

Licensed under [AAGPL-3.0-or-later](LICENSE).
