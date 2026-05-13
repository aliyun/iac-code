from dataclasses import dataclass

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

    @property
    def is_terminal(self) -> bool:
        return self.status.endswith("_COMPLETE") or self.status.endswith("_FAILED")

    @property
    def is_success(self) -> bool:
        return self.status.endswith("_COMPLETE")


@dataclass
class ResourceStatus:
    name: str
    resource_type: str
    status: str
    status_reason: str

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
