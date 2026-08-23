# Preserve pre-split state addresses when count-gating legacy executor-local.

moved {
  from = aws_iam_role.executor_local
  to   = aws_iam_role.executor_local[0]
}

moved {
  from = aws_iam_role_policy.executor_local
  to   = aws_iam_role_policy.executor_local[0]
}

moved {
  from = aws_iam_role_policy_attachment.executor_local_read_only
  to   = aws_iam_role_policy_attachment.executor_local_read_only[0]
}

moved {
  from = aws_iam_role_policy_attachment.executor_local_power_user
  to   = aws_iam_role_policy_attachment.executor_local_power_user[0]
}
