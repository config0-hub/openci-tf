# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
provider "aws" { region = var.aws_region }
data "aws_caller_identity" "current" {}
