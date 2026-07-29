"""Shared ROS template-kind predicates."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_terraform_template(data: Mapping[Any, Any]) -> bool:
    """Return whether a parsed template uses a ROS Terraform/OpenTofu transform."""

    transform = data.get("Transform")
    values = transform if isinstance(transform, list) else [transform]
    return any(
        isinstance(value, str) and value.startswith(("Aliyun::Terraform-", "Aliyun::OpenTofu-")) for value in values
    )
