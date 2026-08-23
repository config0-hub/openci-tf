output "kms_key_arn" { value = aws_kms_key.foundation.arn }
output "kms_alias" { value = aws_kms_alias.foundation.name }
output "tmp_bucket_name" { value = aws_s3_bucket.foundation["tmp"].bucket }
output "tmp_bucket_arn" { value = aws_s3_bucket.foundation["tmp"].arn }
output "package_bucket_name" { value = aws_s3_bucket.foundation["package"].bucket }
output "package_bucket_arn" { value = aws_s3_bucket.foundation["package"].arn }
output "done_bucket_name" { value = aws_s3_bucket.foundation["done"].bucket }
output "done_bucket_arn" { value = aws_s3_bucket.foundation["done"].arn }
