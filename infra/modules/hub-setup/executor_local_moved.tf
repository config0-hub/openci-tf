# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
# executor-local lost its count gate; map indexed state back to the bare address.

moved {
  from = aws_iam_role.executor_local[0]
  to   = aws_iam_role.executor_local
}

moved {
  from = aws_iam_role_policy.executor_local[0]
  to   = aws_iam_role_policy.executor_local
}
