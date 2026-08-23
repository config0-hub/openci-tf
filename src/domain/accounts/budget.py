"""Execution and role-credential budget calculations."""
from src.core.errors import BudgetUnmintableError

# Conservative AWS consult ruling: all safe actions use the same engine path.
# Commands remains the validated folder timeout; fixed overhead is 140 seconds.
DEFAULT_OVERHEAD_SECONDS = (15, 30, 60, 20, 15)
def compute_budget(dispatch: int, queue_estimate: int, install: int, commands: int, upload: int, finalize: int) -> int:
    values = (dispatch, queue_estimate, install, commands, upload, finalize)
    if any(value < 0 for value in values): raise ValueError("budget components must be non-negative")
    return sum(values)
def compute_ttl(budget: int, role_max: int) -> int:
    ceiling = min(role_max, 3600)
    if budget > ceiling: raise BudgetUnmintableError("execution budget exceeds role credential horizon")
    return max(900, budget)


def default_budget_for_action(action: str, command_timeout: int) -> int:
    """Combine conservative engine overhead with a validated folder timeout."""
    if action not in {"plan", "drift", "report", "plan_destroy", "apply", "destroy"}:
        raise ValueError(f"unknown action: {action}")
    dispatch, queue, install, upload, finalize = DEFAULT_OVERHEAD_SECONDS
    return compute_budget(dispatch, queue, install, command_timeout, upload, finalize)
