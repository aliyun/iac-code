from dataclasses import dataclass, field
from typing import Any

from iac_code.i18n import _

# Stack/resource status strings for pybabel extraction.
# Reference: https://help.aliyun.com/zh/ros/developer-reference/api-ros-2019-09-10-getstack
_STACK_STATUS_STRINGS = [
    _("CREATE_IN_PROGRESS"),
    _("CREATE_FAILED"),
    _("CREATE_COMPLETE"),
    _("UPDATE_IN_PROGRESS"),
    _("UPDATE_FAILED"),
    _("UPDATE_COMPLETE"),
    _("DELETE_IN_PROGRESS"),
    _("DELETE_FAILED"),
    _("DELETE_COMPLETE"),
    _("CREATE_ROLLBACK_IN_PROGRESS"),
    _("CREATE_ROLLBACK_FAILED"),
    _("CREATE_ROLLBACK_COMPLETE"),
    _("ROLLBACK_IN_PROGRESS"),
    _("ROLLBACK_FAILED"),
    _("ROLLBACK_COMPLETE"),
    _("CHECK_IN_PROGRESS"),
    _("CHECK_FAILED"),
    _("CHECK_COMPLETE"),
    _("REVIEW_IN_PROGRESS"),
    _("IMPORT_CREATE_IN_PROGRESS"),
    _("IMPORT_CREATE_FAILED"),
    _("IMPORT_CREATE_COMPLETE"),
    _("IMPORT_CREATE_ROLLBACK_IN_PROGRESS"),
    _("IMPORT_CREATE_ROLLBACK_FAILED"),
    _("IMPORT_CREATE_ROLLBACK_COMPLETE"),
    _("IMPORT_UPDATE_IN_PROGRESS"),
    _("IMPORT_UPDATE_FAILED"),
    _("IMPORT_UPDATE_COMPLETE"),
    _("IMPORT_UPDATE_ROLLBACK_IN_PROGRESS"),
    _("IMPORT_UPDATE_ROLLBACK_FAILED"),
    _("IMPORT_UPDATE_ROLLBACK_COMPLETE"),
    # Resource-level statuses (ListStackResources API)
    _("INIT_COMPLETE"),
    _("IMPORT_IN_PROGRESS"),
    _("IMPORT_COMPLETE"),
    _("IMPORT_FAILED"),
]


SUCCESS_STACK_STATUSES = {
    "CREATE_COMPLETE",
    "UPDATE_COMPLETE",
    "IMPORT_CREATE_COMPLETE",
    "IMPORT_UPDATE_COMPLETE",
    "CHECK_COMPLETE",
}


def translate_status(status: str) -> str:
    """Translate a stack/resource status code to localized display text."""
    return _(status)


@dataclass
class StackStatus:
    stack_id: str
    stack_name: str
    status: str
    status_reason: str
    progress_percentage: float
    outputs: dict[str, object] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.endswith("_COMPLETE") or self.status.endswith("_FAILED")

    @property
    def is_success(self) -> bool:
        return self.status in SUCCESS_STACK_STATUSES


@dataclass
class ResourceStatus:
    name: str
    resource_type: str
    status: str
    status_reason: str
    # The API field may contain a name (RAM Role, OSS Bucket), not an ID.
    physical_resource_id: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    account_scope: str = ""
    console_partition: str = ""

    def to_progress_dict(self) -> dict[str, Any]:
        """Keep legacy progress fields and add only observed locator facts."""
        result: dict[str, Any] = {
            "name": self.name,
            "resource_type": self.resource_type,
            "status": self.status,
            "status_reason": self.status_reason,
        }
        if self.physical_resource_id:
            result["physical_resource_id"] = self.physical_resource_id
        if self.attributes:
            result["attributes"] = dict(self.attributes)
        if self.account_scope:
            result["account_scope"] = self.account_scope
        if self.console_partition:
            result["console_partition"] = self.console_partition
        return result

    @property
    def status_icon(self) -> str:
        if self.status.endswith("_COMPLETE"):
            return "\u2705"
        if "IN_PROGRESS" in self.status:
            return "\u23f3"
        if self.status.endswith("_FAILED"):
            return "\u274c"
        if "DELETE" in self.status:
            return "\U0001f5d1\ufe0f"
        return "\u2b1a"


@dataclass
class InstanceStatus:
    account_id: str
    region_id: str
    status: str
    status_reason: str
    elapsed_seconds: int

    @property
    def status_icon(self) -> str:
        if self.status in ("SUCCEEDED", "CURRENT"):
            return "\u2705"
        if self.status in ("RUNNING",):
            return "\u23f3"
        if self.status in ("FAILED",):
            return "\u274c"
        return "\u2b1a"
