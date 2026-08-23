# Terraform test modules

Small AWS modules used by the openci-tf sample GitOps repository. The modules
are intentionally simple, independent, and low-cost while idle.

```text
modules/
  vpc-basic/              VPC, internet gateway, public subnet, route table
  dynamodb-table/         One pay-per-request DynamoDB table
  sqs-queue/              One standard SQS queue
  cloudwatch-log-group/   One CloudWatch log group
  s3-bucket/              One random-suffixed private S3 bucket
  sns-topic/              One SNS topic
  eventbridge-rule/       One disabled EventBridge schedule rule
```

Each module accepts a `tags` map and exposes basic IDs/ARNs for assertions.
