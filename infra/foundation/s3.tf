locals {
  # ref 4353245 - openci-tf remote executor consistency naming
  # Deterministic names: <prefix>-{tmp,package,done}-<account-id>. Downstream
  # stacks discover these via data sources; do not make them configurable.
  account_id          = data.aws_caller_identity.current.account_id
  tmp_bucket_name     = "${var.name_prefix}-tmp-${local.account_id}"
  package_bucket_name = "${var.name_prefix}-package-${local.account_id}"
  done_bucket_name    = "${var.name_prefix}-done-${local.account_id}"
  openci_tf_prefix       = "openci-tf/"
  buckets = {
    tmp     = { name = local.tmp_bucket_name, expiration = var.tmp_lifecycle_days, versioned = false, threshold = var.tmp_size_alarm_bytes }
    package = { name = local.package_bucket_name, expiration = var.package_lifecycle_days, versioned = true, threshold = var.package_size_alarm_bytes }
    done    = { name = local.done_bucket_name, expiration = var.done_expiration_days, versioned = true, threshold = var.done_size_alarm_bytes }
  }
}
resource "aws_s3_bucket" "foundation" {
  for_each = local.buckets
  bucket   = each.value.name
  # Operator install/uninstall journey: buckets hold only transient run artifacts.
  force_destroy = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "foundation" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.foundation[each.key].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.foundation.arn
    }
  }
}
resource "aws_s3_bucket_public_access_block" "foundation" {
  for_each                = local.buckets
  bucket                  = aws_s3_bucket.foundation[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "foundation" {
  for_each = { for name, bucket in local.buckets : name => bucket if bucket.versioned }
  bucket   = aws_s3_bucket.foundation[each.key].id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_lifecycle_configuration" "foundation" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.foundation[each.key].id
  rule {
    id     = "retention"
    status = "Enabled"
    filter {}
    expiration { days = each.value.expiration }
  }
  dynamic "rule" {
    for_each = each.key == "tmp" ? [true] : []
    content {
      id     = "openci-tf-artifact-retention"
      status = "Enabled"
      filter { prefix = local.openci_tf_prefix }
      expiration { days = var.plan_retention_days }
    }
  }
}
resource "aws_cloudwatch_metric_alarm" "bucket_size" {
  for_each            = local.buckets
  alarm_name          = "${var.name_prefix}-${each.key}-size"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "BucketSizeBytes"
  namespace           = "AWS/S3"
  period              = 86400
  statistic           = "Average"
  threshold           = each.value.threshold
  dimensions = {
    BucketName  = aws_s3_bucket.foundation[each.key].bucket
    StorageType = "StandardStorage"
  }
}
