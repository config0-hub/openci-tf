# openci-tf-settings stores both repository and account-alias rows.  Keeping a
# composite key makes the row kinds explicit and lets account aliases share the
# same audited settings table.
resource "aws_dynamodb_table" "settings" {
  name         = "${var.project_name}-settings"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  tags = merge(var.tags, {
    Name = "${var.project_name}-settings"
  })
}

resource "aws_dynamodb_table" "locks" {
  name         = "${var.project_name}-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
  tags = merge(var.tags, { Name = "${var.project_name}-locks" })
}

resource "aws_dynamodb_table" "run_registry" {
  name         = "${var.project_name}-run-registry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  attribute {
    name = "gsi1pk"
    type = "S"
  }

  attribute {
    name = "gsi1sk"
    type = "S"
  }

  attribute {
    name = "gsi2pk"
    type = "S"
  }

  attribute {
    name = "gsi2sk"
    type = "S"
  }

  global_secondary_index {
    name            = "repo_created"
    hash_key        = "gsi1pk"
    range_key       = "gsi1sk"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "pipeline_apply_step"
    hash_key        = "gsi2pk"
    range_key       = "gsi2sk"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "expire_ttl"
    enabled        = true
  }

  tags = merge(var.tags, { Name = "${var.project_name}-run-registry" })
}
