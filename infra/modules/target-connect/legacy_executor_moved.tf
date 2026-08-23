# Preserve pre-split state addresses when count-gating legacy executor-remote.

moved {
  from = aws_iam_role.executor_remote
  to   = aws_iam_role.executor_remote[0]
}

moved {
  from = aws_iam_role_policy.executor_remote
  to   = aws_iam_role_policy.executor_remote[0]
}

moved {
  from = aws_iam_role_policy_attachment.executor_remote_read_only
  to   = aws_iam_role_policy_attachment.executor_remote_read_only[0]
}

moved {
  from = aws_iam_role_policy_attachment.executor_remote_power_user
  to   = aws_iam_role_policy_attachment.executor_remote_power_user[0]
}
