# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain errors that callers must handle explicitly."""

class BudgetUnmintableError(RuntimeError): pass
class PayloadTooLargeError(ValueError): pass
class ConfigValidationError(ValueError): pass
class LockHeldError(RuntimeError): pass
class EngineAckError(RuntimeError): pass
class SignerHorizonExceededError(RuntimeError): pass
class PollDeadlineExceededError(RuntimeError): pass
class DeadlineExceededError(RuntimeError): pass
class TriggerMismatchError(RuntimeError): pass
class MalformedResultError(ValueError): pass
class DoneMarkerTooLargeError(ValueError): pass
class CredentialExpiredError(RuntimeError): pass
class ConfigResolutionError(ValueError): pass
class SsmEnvError(ValueError): pass
