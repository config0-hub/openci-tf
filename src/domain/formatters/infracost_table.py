"""Render Infracost JSON breakdowns into original-style ASCII tables."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_TRUNCATION_NOTE = "\n[... cost table truncated; paid rows and totals retained ...]\n"

_NAME_WIDTH = 58
_QTY_WIDTH = 12
_UNIT_WIDTH = 14
_PRICE_WIDTH = 12
_COST_WIDTH = 14


@dataclass(frozen=True)
class _Row:
    name: str
    indent: str
    qty: str
    unit: str
    price: str
    monthly: str
    paid: bool
    priority: int  # lower = keep first when truncating


@dataclass(frozen=True)
class _RowBlock:
    rows: list[_Row]

    @property
    def has_paid(self) -> bool:
        return any(row.paid for row in self.rows)

    @property
    def is_header(self) -> bool:
        return any(row.name.startswith("Project:") for row in self.rows)

    @property
    def is_total(self) -> bool:
        return any(row.name in {"PROJECT TOTAL", "OVERALL TOTAL"} for row in self.rows)


def _strip_control(text: str) -> str:
    text = re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)
    return "".join(ch for ch in text if ch == "\n" or ch >= " ")


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _is_paid_amount(value: object) -> bool:
    amount = _as_float(value)
    return amount is not None and amount > 0


def _format_money(value: object) -> str:
    amount = _as_float(value)
    if amount is None:
        return ""
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"


def _format_qty(value: object) -> str:
    amount = _as_float(value)
    if amount is None:
        return ""
    if amount == int(amount):
        return str(int(amount))
    text = f"{amount:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _format_price(value: object) -> str:
    amount = _as_float(value)
    if amount is None:
        return ""
    if amount == 0:
        return "0"
    text = f"{amount:.12f}".rstrip("0").rstrip(".")
    return text or "0"


def _usage_message(component: dict[str, Any]) -> str | None:
    if component.get("priceNotFound"):
        return "not found"
    if component.get("usageBased"):
        price = _format_price(component.get("price"))
        unit = str(component.get("unit") or "").strip()
        if price and unit:
            return f"Monthly cost depends on usage: ${price} per {unit}"
        if price:
            return f"Monthly cost depends on usage: ${price}"
        return "Monthly cost depends on usage"
    monthly = _as_float(component.get("monthlyCost"))
    qty = _as_float(component.get("monthlyQuantity"))
    price = _as_float(component.get("price"))
    if monthly is None and qty is None and price is not None:
        unit = str(component.get("unit") or "").strip()
        formatted = _format_price(price)
        if unit:
            return f"Monthly cost depends on usage: ${formatted} per {unit}"
        return f"Monthly cost depends on usage: ${formatted}"
    return None


def _component_row(name: str, indent: str, component: dict[str, Any]) -> _Row:
    usage = _usage_message(component)
    monthly_amount = _as_float(component.get("monthlyCost"))
    paid = monthly_amount is not None and monthly_amount > 0
    if usage:
        return _Row(name, indent, "", "", "", usage, paid, 2 if paid else 3)
    return _Row(
        name,
        indent,
        _format_qty(component.get("monthlyQuantity")),
        str(component.get("unit") or ""),
        _format_price(component.get("price")),
        _format_money(component.get("monthlyCost")),
        paid,
        1 if paid else 4,
    )


def _append_resource_rows(rows: list[_Row], resource: dict[str, Any], *, indent: str = "") -> None:
    name = str(resource.get("name") or resource.get("address") or "resource")
    rows.append(_Row(name, indent, "", "", "", "", False, 0))
    components = resource.get("costComponents") or []
    subresources = resource.get("subresources") or []
    children_count = len(components) + len(subresources)
    child_index = 0
    for component in components:
        is_last = child_index == children_count - 1
        branch = "└─ " if is_last else "├─ "
        rows.append(_component_row(str(component.get("name") or "component"), indent + branch, component))
        child_index += 1
    for subresource in subresources:
        is_last = child_index == children_count - 1
        branch = "└─ " if is_last else "├─ "
        child_indent = indent + ("   " if is_last else "│  ")
        sub_name = str(subresource.get("name") or "subresource")
        rows.append(_Row(sub_name, indent + branch, "", "", "", "", False, 0))
        sub_components = subresource.get("costComponents") or []
        for sub_index, component in enumerate(sub_components):
            sub_last = sub_index == len(sub_components) - 1
            sub_branch = "└─ " if sub_last else "├─ "
            rows.append(_component_row(str(component.get("name") or "component"), child_indent + sub_branch, component))
        child_index += 1


def _resource_block(resource: dict[str, Any]) -> _RowBlock:
    rows: list[_Row] = []
    _append_resource_rows(rows, resource)
    return _RowBlock(rows)


def _project_blocks(project: dict[str, Any]) -> list[_RowBlock]:
    blocks: list[_RowBlock] = []
    display = str(project.get("displayName") or project.get("name") or "project")
    blocks.append(_RowBlock([_Row(f"Project: {display}", "", "", "", "", "", False, 0)]))
    breakdown = project.get("breakdown") or {}
    resources = breakdown.get("resources") or []
    if not resources:
        blocks.append(_RowBlock([_Row("(no supported resources)", "  ", "", "", "", "", False, 5)]))
    for resource in resources:
        blocks.append(_resource_block(resource))
    subtotal = breakdown.get("totalMonthlyCost")
    if subtotal is not None:
        paid = _is_paid_amount(subtotal)
        blocks.append(_RowBlock([_Row("PROJECT TOTAL", "", "", "", "", _format_money(subtotal), paid, 0)]))
    return blocks


def _format_row(row: _Row) -> str:
    label = f"{row.indent}{row.name}"
    if not row.qty and not row.unit and not row.price and not row.monthly:
        return label.rstrip()
    if row.monthly and not row.qty and not row.unit and not row.price:
        if row.monthly.startswith("Monthly cost") or row.monthly == "not found":
            return f"{label:<{_NAME_WIDTH}}{row.monthly}"
        return (
            f"{label:<{_NAME_WIDTH}}"
            f"{'':>{_QTY_WIDTH}}  "
            f"{'':<{_UNIT_WIDTH}}"
            f"{'':>{_PRICE_WIDTH}}  "
            f"{row.monthly:>{_COST_WIDTH}}"
        )
    return (
        f"{label:<{_NAME_WIDTH}}"
        f"{row.qty:>{_QTY_WIDTH}}  "
        f"{row.unit:<{_UNIT_WIDTH}}"
        f"{row.price:>{_PRICE_WIDTH}}  "
        f"{row.monthly:>{_COST_WIDTH}}"
    )


def _table_header() -> str:
    return (
        f"{'Name':<{_NAME_WIDTH}}"
        f"{'Monthly Qty':>{_QTY_WIDTH}}  "
        f"{'Unit':<{_UNIT_WIDTH}}"
        f"{'Unit price':>{_PRICE_WIDTH}}  "
        f"{'Monthly cost':>{_COST_WIDTH}}"
    )


def _truncate_blocks(blocks: list[_RowBlock], *, max_rows: int) -> tuple[list[_Row], bool]:
    if sum(len(block.rows) for block in blocks) <= max_rows:
        return [row for block in blocks for row in block.rows], False

    headers = [block for block in blocks if block.is_header]
    totals = [block for block in blocks if block.is_total]
    body = [block for block in blocks if not block.is_header and not block.is_total]
    paid = [block for block in body if block.has_paid]
    remainder = [block for block in body if not block.has_paid]
    reserved = sum(len(block.rows) for block in headers + totals)
    budget = max(max_rows - reserved, 0)

    kept: list[_RowBlock] = []
    used = 0
    for block in paid + remainder:
        if used + len(block.rows) > budget:
            continue
        kept.append(block)
        used += len(block.rows)

    selected = headers + kept + totals
    return [row for block in selected for row in block.rows], True


def render_infracost_table(text: str, *, max_rows: int = 500) -> str:
    """Convert Infracost JSON into a deterministic ASCII table."""
    clean = _strip_control(text or "").strip()
    if not clean:
        return "Cost data unavailable."
    try:
        data = json.loads(clean)
    except json.JSONDecodeError:
        return "Cost data unavailable (invalid JSON)."

    if data.get("skipped"):
        reason = str(data.get("reason") or "not configured")
        return f"Cost analysis: {reason}"

    projects = data.get("projects") or []
    if not projects and data.get("totalMonthlyCost") is None:
        return "Cost data unavailable (empty breakdown)."

    blocks: list[_RowBlock] = []
    for project in projects:
        blocks.extend(_project_blocks(project))
        if len(projects) > 1:
            blocks.append(_RowBlock([_Row("", "", "", "", "", "", False, 5)]))

    total = data.get("totalMonthlyCost")
    if total is not None:
        paid = _is_paid_amount(total)
        blocks.append(_RowBlock([_Row("OVERALL TOTAL", "", "", "", "", _format_money(total), paid, 0)]))

    rows, truncated = _truncate_blocks(blocks, max_rows=max_rows)
    lines = [_table_header(), ""] + [_format_row(row) for row in rows]
    rendered = "\n".join(lines).rstrip()
    if truncated:
        rendered += _TRUNCATION_NOTE
    return rendered
