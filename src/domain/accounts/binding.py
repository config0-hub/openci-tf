# SPDX-FileCopyrightText: 2026 Config0, Inc.
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frozen target-account bindings carried from resolution into execution."""
from __future__ import annotations

from dataclasses import dataclass

from src.core.errors import ConfigResolutionError
from src.domain.accounts.aliases import AccountAlias


@dataclass(frozen=True)
class AccountBinding:
    """The alias-derived values that must not change during one run or intent."""

    account_id: str
    readonly_role_name: str
    poweruser_role_name: str | None
    external_id: str
    max_ttl: int

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "readonly_role_name": self.readonly_role_name,
            "poweruser_role_name": self.poweruser_role_name,
            "external_id": self.external_id,
            "max_ttl": self.max_ttl,
        }

    def to_compact(self) -> list[object]:
        """Serialize repeat-heavy outer Map state without duplicating field names."""
        return [
            self.readonly_role_name,
            self.poweruser_role_name,
            self.external_id,
            self.max_ttl,
        ]


_ACCOUNT_ID_LENGTH = 12


def _validated_account_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != _ACCOUNT_ID_LENGTH or not value.isdecimal():
        raise ConfigResolutionError("frozen account binding has invalid account_id")
    return value


def _validated_role_name(value: object, *, field: str, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigResolutionError(f"frozen account binding has invalid {field}")
    return value


def account_binding_from_alias(alias: AccountAlias) -> AccountBinding:
    """Freeze one validated alias lookup for later consumers."""
    if not isinstance(alias.external_id, str) or not alias.external_id:
        raise ConfigResolutionError("account alias is missing its derived external_id")
    max_ttl = alias.max_ttl if alias.max_ttl is not None else 3600
    return AccountBinding(
        account_id=_validated_account_id(alias.account_id),
        readonly_role_name=str(
            _validated_role_name(
                alias.role_name,
                field="readonly_role_name",
                required=True,
            )
        ),
        poweruser_role_name=_validated_role_name(
            alias.poweruser_role_name,
            field="poweruser_role_name",
            required=False,
        ),
        external_id=alias.external_id,
        max_ttl=max_ttl,
    )


def account_binding_from_dict(raw: object) -> AccountBinding:
    """Validate a serialized binding without consulting the mutable alias table."""
    if not isinstance(raw, dict):
        raise ConfigResolutionError("frozen account_binding is required")
    max_ttl = raw.get("max_ttl")
    if isinstance(max_ttl, bool) or not isinstance(max_ttl, int) or max_ttl < 900:
        raise ConfigResolutionError("frozen account binding has invalid max_ttl")
    external_id = raw.get("external_id")
    if not isinstance(external_id, str) or not external_id:
        raise ConfigResolutionError("frozen account binding has invalid external_id")
    return AccountBinding(
        account_id=_validated_account_id(raw.get("account_id")),
        readonly_role_name=str(
            _validated_role_name(
                raw.get("readonly_role_name"),
                field="readonly_role_name",
                required=True,
            )
        ),
        poweruser_role_name=_validated_role_name(
            raw.get("poweruser_role_name"),
            field="poweruser_role_name",
            required=False,
        ),
        external_id=external_id,
        max_ttl=max_ttl,
    )


def account_binding_from_compact(raw: object, account_id: object) -> AccountBinding:
    """Validate the compact binding carried by a Step Functions Map item."""
    if not isinstance(raw, list) or len(raw) != 4:
        raise ConfigResolutionError("compact frozen account_binding is required")
    readonly_role_name, poweruser_role_name, external_id, max_ttl = raw
    return account_binding_from_dict(
        {
            "account_id": account_id,
            "readonly_role_name": readonly_role_name,
            "poweruser_role_name": poweruser_role_name,
            "external_id": external_id,
            "max_ttl": max_ttl,
        }
    )
