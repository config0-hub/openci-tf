"""Validated account-alias settings."""
import re
from dataclasses import dataclass
from decimal import Decimal

from src.core.errors import ConfigValidationError
from src.platform.aws.dynamo import get_account_alias

_ACCOUNT = re.compile(r"^\d{12}$"); _ROLE = re.compile(r"^[A-Za-z0-9+=,.@_-]{1,64}$"); _EXTERNAL_ID = re.compile(r"^openci-tf-[0-9a-f]{16}$")
_DEFAULT_READONLY_ROLE = "openci-tf-executor-readonly"
_LEGACY_READONLY_ROLE = "openci-tf-executor-remote"
_DEFAULT_POWERUSER_ROLE = "openci-tf-executor-poweruser"

@dataclass(frozen=True)
class AccountAlias:
    account_id: str
    role_name: str
    poweruser_role_name: str | None
    external_id: str | None
    max_ttl: int | None
    enable_apply: bool

def load_account_alias(alias: str) -> AccountAlias:
    try:
        row = get_account_alias(alias)
    except ValueError as error:
        raise ConfigValidationError(str(error)) from error
    account_id = row.get("account_id")
    role_name = row.get("role_name", _LEGACY_READONLY_ROLE)
    poweruser_role_name = row.get("poweruser_role_name")
    max_ttl = row.get("max_ttl")
    if not isinstance(account_id, str) or not _ACCOUNT.fullmatch(account_id): raise ConfigValidationError("account alias has invalid account_id")
    if not isinstance(role_name, str) or not _ROLE.fullmatch(role_name): raise ConfigValidationError("account alias has invalid role_name")
    if poweruser_role_name is not None and (not isinstance(poweruser_role_name, str) or not _ROLE.fullmatch(poweruser_role_name)):
        raise ConfigValidationError("account alias has invalid poweruser_role_name")
    if isinstance(max_ttl, Decimal):
        if max_ttl != max_ttl.to_integral_value():
            raise ConfigValidationError("account alias has invalid max_ttl")
        max_ttl = int(max_ttl)
    if max_ttl is not None and (not isinstance(max_ttl, int) or isinstance(max_ttl, bool) or max_ttl < 900): raise ConfigValidationError("account alias has invalid max_ttl")
    external_id = row.get("external_id")
    if external_id is not None and (not isinstance(external_id, str) or not _EXTERNAL_ID.fullmatch(external_id)): raise ConfigValidationError("account alias has invalid external_id")
    enable_apply = row.get("enable_apply", False) is True
    if poweruser_role_name is None and enable_apply:
        poweruser_role_name = _DEFAULT_POWERUSER_ROLE
    return AccountAlias(account_id, role_name, poweruser_role_name, external_id, max_ttl, enable_apply)
