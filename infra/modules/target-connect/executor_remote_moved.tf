# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# executor-remote lost its count gate; map indexed state back to the bare address.

moved {
  from = aws_iam_role.executor_remote[0]
  to   = aws_iam_role.executor_remote
}

moved {
  from = aws_iam_role_policy.executor_remote[0]
  to   = aws_iam_role_policy.executor_remote
}
