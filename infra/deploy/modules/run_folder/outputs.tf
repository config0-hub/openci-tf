# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
output "state_machine_arn" { value = aws_sfn_state_machine.this.arn }
