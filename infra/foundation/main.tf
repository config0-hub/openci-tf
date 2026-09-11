# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# The default provider's default_tags cannot reference
# data.aws_caller_identity.current directly: that data source uses this same
# provider, so the provider config would depend on itself (a cycle). A
# second aliased provider instance reads the account id instead.
provider "aws" {
  alias  = "config0_identity"
  region = var.aws_region
}

data "aws_caller_identity" "config0_identity" {
  provider = aws.config0_identity
}

locals {
  config0_tags = {
    "config0:managed" = "true"
    "config0:addon"   = "openci-tf"
    "config0:tenant"  = data.aws_caller_identity.config0_identity.account_id
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(var.tags, local.config0_tags)
  }
}

data "aws_caller_identity" "current" {}
