"""Authorization policy independent of the GitHub transport."""
def can_trigger(permission: str) -> bool:
    return permission.lower() in {"write", "admin"}
