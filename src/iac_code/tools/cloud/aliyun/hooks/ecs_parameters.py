"""Narrow compatibility adapters for ECS price queries."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from iac_code.tools.cloud.aliyun.api_contract import ApiContractError
from iac_code.tools.cloud.aliyun.api_hooks import parameter_normalizer, register_action_alias

if TYPE_CHECKING:
    from iac_code.tools.cloud.aliyun.api_contract import CanonicalWireContract

_SYSTEM_DISK_ALIASES = {
    "SystemDisk.1.Category": "SystemDisk.Category",
    "SystemDisk.1.Size": "SystemDisk.Size",
    "SystemDisk.1.PerformanceLevel": "SystemDisk.PerformanceLevel",
    "SystemDisk.Capacity": "SystemDisk.Size",
    "SystemDiskCategory": "SystemDisk.Category",
    "SystemDiskSize": "SystemDisk.Size",
    "SystemDiskPerformanceLevel": "SystemDisk.PerformanceLevel",
}
_SYSTEM_DISK_FIELDS = {"Category", "Size", "PerformanceLevel"}

register_action_alias("ecs", "DescribePrices", "DescribePrice")


@parameter_normalizer("ecs", "DescribePrice")
def normalize_ecs_price_parameters(
    product: str,
    action: str,
    params: dict[str, Any],
    *,
    contract: CanonicalWireContract | None = None,
) -> None:
    """Normalize known DescribePrice aliases on the caller's copied parameters."""
    del product, action
    units = [
        "Month" if params[key] == "Monthly" else params[key] for key in ("PriceUnit", "PeriodType") if key in params
    ]
    if len(units) == 2 and (type(units[0]) is not type(units[1]) or units[0] != units[1]):
        raise ApiContractError("conflicting_parameter_alias", parameter="PriceUnit")
    aliases = [
        (alias, canonical, params[alias]) for alias, canonical in _SYSTEM_DISK_ALIASES.items() if alias in params
    ]
    system_disk = params.get("SystemDisk")
    # Flatten only the known object shape; do not discard unknown fields.
    if isinstance(system_disk, dict) and system_disk and system_disk.keys() <= _SYSTEM_DISK_FIELDS:
        aliases.extend(("SystemDisk", f"SystemDisk.{field}", value) for field, value in system_disk.items())
    # Check every conflict before changing any key; never silently choose an input.
    values = {canonical: params[canonical] for _, canonical, _ in aliases if canonical in params}
    for _, canonical, value in aliases:
        if canonical in values:
            if type(value) is not type(values[canonical]) or value != values[canonical]:
                raise ApiContractError("conflicting_parameter_alias", parameter=canonical)
        values[canonical] = value
    for alias, canonical, value in aliases:
        params.pop(alias, None)
        params[canonical] = value
    if units:
        params.pop("PeriodType", None)
        params["PriceUnit"] = units[0]

    if contract is None or contract.style != "RPC":
        return
    metadata = {parameter.name: parameter for parameter in contract.parameters}
    for name, value in params.items():
        parameter = metadata.get(name)
        # Integer text has the same RPC wire value; all normal schema checks follow.
        if (
            parameter is not None
            and parameter.location == "query"
            and parameter.schema is not None
            and parameter.schema.get("type") == "integer"
            and isinstance(value, str)
            and re.fullmatch(r"(?:0|-?[1-9][0-9]{0,18})", value)
        ):
            params[name] = int(value)
