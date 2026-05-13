"""Permission types for the tool system."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class PermissionMode(str, Enum):
    """Permission mode."""

    DEFAULT = "default"  # Write operations require user confirmation
    PLAN = "plan"  # Read-only operations auto-allowed
    AUTO = "auto"  # All operations auto-allowed


@dataclass
class PermissionResult:
    """Permission check result."""

    behavior: Literal["allow", "deny", "ask"]
    message: str = ""


PermissionDecision = Literal["always_allow", "always_deny"]
