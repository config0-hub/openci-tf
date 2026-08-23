resource "aws_dynamodb_table" "tracer" {
  name         = "${local.name_prefix}-tracer"
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

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = {
    Name = "${local.name_prefix}-tracer"
  }

  depends_on = [terraform_data.account_guard]
}
