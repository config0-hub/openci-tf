"""Presign lifetime cannot outlive the signing credentials."""
from src.core.errors import SignerHorizonExceededError
SIGNER_CREDENTIAL_HORIZON_SECONDS = 3600  # Human may lower before real-AWS use.
def effective_horizon(constant: int = SIGNER_CREDENTIAL_HORIZON_SECONDS, credentials: object | None = None) -> int:
    expiry = getattr(credentials, "_expiry_time", None)
    if expiry is None: return constant
    try:
        import datetime
        seconds = int((expiry - datetime.datetime.now(expiry.tzinfo)).total_seconds())
        return min(constant, max(0, seconds))
    except (AttributeError, TypeError): return constant
def validate_presign_budget(budget: int, horizon: int) -> None:
    if budget <= 0 or budget > horizon: raise SignerHorizonExceededError(f"presign budget {budget} exceeds signer horizon {horizon}")
